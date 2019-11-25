import numpy as np
import scipy as sp
import pandas as pd
import numbers
from typing import Callable, List, Union
import logging

from .base import Epsilon
from ..distance import SCALE_LIN
from ..sampler import Sampler

logger = logging.getLogger("Epsilon")


class Temperature(Epsilon):
    """
    A temperature scheme handles the decrease of the temperatures employed
    by a :class:`pyabc.acceptor.StochasticAcceptor` over time.

    Parameters
    ----------
    schemes: Union[Callable, List[Callable]], optional
        Temperature schemes returning proposed
        temperatures for the next time point, e.g.
        instances of :class:`pyabc.epsilon.TemperatureScheme`.
    aggregate_fun: Callable[List[float], float], optional
        The function to aggregate the schemes by, of the form
        ``Callable[List[float], float]``.
        Defaults to taking the minimum.
    initial_temperature: float, optional
        The initial temperature. If None provided, an AcceptanceRateScheme
        is used.
    enforce_exact_final_temperature: bool, optional
        Whether to force the final temperature (if max_nr_populations < inf)
        to be 1.0, giving exact inference.

    Properties
    ----------
    max_nr_populations: int
        The maximum number of iterations as passed to ABCSMC.
        May be inf, but not all schemes can handle that (and will complain).
    temperatures: Dict[int, float]
        Times as keys and temperatures as values.
    """

    def __init__(
            self,
            schemes: Union[Callable, List[Callable]] = None,
            aggregate_fun: Callable[[List[float]], float] = None,
            initial_temperature: float = None,
            enforce_exact_final_temperature: bool = True):
        self.schemes = schemes

        if aggregate_fun is None:
            # use minimum over all proposed temperature values
            aggregate_fun = min
        self.aggregate_fun = aggregate_fun

        if initial_temperature is None:
            initial_temperature = AcceptanceRateScheme()
        self.initial_temperature = initial_temperature

        self.enforce_exact_final_temperature = enforce_exact_final_temperature

        # to be filled later
        self.max_nr_populations = None
        self.temperatures = {}

    def initialize(self,
                   t: int,
                   get_weighted_distances: Callable[[], pd.DataFrame],
                   get_all_records: Callable[[], List[dict]],
                   max_nr_populations: int,
                   acceptor_config: dict):
        self.max_nr_populations = max_nr_populations

        # set default schemes
        if self.schemes is None:
            # this combination proved rather stable
            acc_rate_scheme = AcceptanceRateScheme()
            decay_scheme = (
                ExpDecayFixedIterScheme() if np.isfinite(max_nr_populations)
                else ExpDecayFixedRatioScheme())
            self.schemes = [acc_rate_scheme, decay_scheme]

        # set initial temperature for time t
        self._update(t, get_weighted_distances, get_all_records,
                     1.0, acceptor_config)

    def configure_sampler(self, sampler: Sampler):
        if callable(self.initial_temperature):
            self.initial_temperature.configure_sampler(sampler)
        for scheme in self.schemes:
            scheme.configure_sampler(sampler)

    def update(self,
               t: int,
               get_weighted_distances: Callable[[], pd.DataFrame],
               get_all_records: Callable[[], List[dict]],
               acceptance_rate: float,
               acceptor_config: dict):
        # set temperature for time t
        self._update(t, get_weighted_distances,
                     get_all_records, acceptance_rate,
                     acceptor_config)

    def _update(self,
                t: int,
                get_weighted_distances: Callable[[], pd.DataFrame],
                get_all_records: Callable[[], List[dict]],
                acceptance_rate: float,
                acceptor_config):
        """
        Compute the temperature for time `t`.
        """
        # scheme arguments
        kwargs = dict(
            t=t,
            get_weighted_distances=get_weighted_distances,
            get_all_records=get_all_records,
            max_nr_populations=self.max_nr_populations,
            pdf_norm=acceptor_config['pdf_norm'],
            kernel_scale=acceptor_config['kernel_scale'],
            prev_temperature=self.temperatures.get(t-1, None),
            acceptance_rate=acceptance_rate,
        )

        if t >= self.max_nr_populations - 1 \
                and self.enforce_exact_final_temperature:
            # t is last time
            temperature = 1.0
        elif not self.temperatures:  # need an initial value
            if callable(self.initial_temperature):
                # execute scheme
                temperature = self.initial_temperature(**kwargs)
            elif isinstance(self.initial_temperature, numbers.Number):
                temperature = self.initial_temperature
            else:
                raise ValueError(
                    "Initial temperature must be a float or a callable")
        else:
            # evaluate schemes
            temps = []
            for scheme in self.schemes:
                temp = scheme(**kwargs)
                temps.append(temp)
            logger.debug(f"Proposed temperatures: {temps}.")

            # compute next temperature based on proposals and fallback
            # should not be higher than before
            fallback = self.temperatures[t-1] \
                if t-1 in self.temperatures else np.inf
            proposed_value = self.aggregate_fun(temps)
            # also a value lower than 1.0 does not make sense
            temperature = max(min(proposed_value, fallback), 1.0)

        if not np.isfinite(temperature):
            raise ValueError("Temperature must be finite.")
        # record found value
        self.temperatures[t] = temperature

    def __call__(self,
                 t: int) -> float:
        return self.temperatures[t]


class TemperatureScheme:
    """
    A TemperatureScheme suggests the next temperature value. It is used as
    one of potentially multiple schemes employed in the Temperature class.
    This class is abstract.

    Parameters
    ----------
    t:
        The time to compute for.
    get_weighted_distances:
        Callable to obtain the weights and kernel values to be used for
        the scheme.
    get_all_records:
        Callable returning a List[dict] of all recorded particles.
    max_nr_populations:
        The maximum number of populations that are supposed to be taken.
    pdf_norm:
        The normalization constant c that will be used in the acceptance step.
    kernel_scale:
        Scale on which the pdf values are (linear or logarithmic).
    prev_temperature:
        The temperature that was used last time (or None if not applicable).
    acceptance_rate:
        The recently obtained rate.
    """

    def __init__(self):
        pass

    def configure_sampler(self, sampler: Sampler):
        """
        Modify the sampler. As in, and redirected from,
        :func:`pyabc.epsilon.Temperature.configure_sampler`.
        """

    def __call__(self,
                 t: int,
                 get_weighted_distances: Callable[[], pd.DataFrame],
                 get_all_records: Callable[[], List[dict]],
                 max_nr_populations: int,
                 pdf_norm: float,
                 kernel_scale: str,
                 prev_temperature: float,
                 acceptance_rate: float):
        pass


class AcceptanceRateScheme(TemperatureScheme):
    """
    Try to keep the acceptance rate constant at a value of
    `target_rate`. Note that this scheme will fail to
    reduce the temperature sufficiently in later iterations, if the
    problem's inherent acceptance rate is lower, but it has been
    observed to give big feasible temperature leaps in early iterations.
    In particular, this scheme can be used to propose an initial temperature.

    Parameters
    ----------
    target_rate: float
        The target acceptance rate to match.
    """

    def __init__(self, target_rate: float = 0.3):
        self.target_rate = target_rate

    def configure_sampler(self, sampler: Sampler):
        sampler.sample_factory.record_rejected = True

    def __call__(self,
                 t: int,
                 get_weighted_distances: Callable[[], pd.DataFrame],
                 get_all_records: Callable[[], List[dict]],
                 max_nr_populations: int,
                 pdf_norm: float,
                 kernel_scale: str,
                 prev_temperature: float,
                 acceptance_rate: float):
        # execute function (expensive if in calibration)
        records = get_all_records()
        # convert to dataframe for easier extraction
        records = pd.DataFrame(records)

        # previous and current transition densities
        t_pd_prev = np.array(records['transition_pd_prev'], dtype=float)
        t_pd = np.array(records['transition_pd'], dtype=float)
        # acceptance kernel likelihoods
        pds = np.array(records['distance'], dtype=float)

        # compute importance weights
        weights = t_pd / t_pd_prev
        # len would suffice, but maybe rather not rely on things to be normed
        weights /= sum(weights)

        temperature = match_acceptance_rate(
            weights, pds, pdf_norm, kernel_scale, self.target_rate)

        return temperature


def match_acceptance_rate(
        weights, pds, pdf_norm, kernel_scale, target_rate):
    """
    For large temperature, changes become effective on an exponential scale,
    thus we optimize the logarithm of the inverse temperature beta.

    For a temperature close to 1, subtler changes are neccesary, however here
    the logarhtm is nearly linear anyway.
    """
    # objective function which we wish to find a root for
    def obj(b):
        beta = np.exp(b)

        # compute rescaled posterior densities
        if kernel_scale == SCALE_LIN:
            acc_probs = (pds / pdf_norm) ** beta
        else:  # kernel_scale == SCALE_LOG
            acc_probs = np.exp((pds - pdf_norm) * beta)

        # to acceptance probabilities to be sure
        acc_probs = np.minimum(acc_probs, 1.0)

        # objective function
        val = np.sum(weights * acc_probs) - target_rate
        return val

    # TODO the lower boundary min_b is somewhat arbitrary
    min_b = -100
    if obj(0) > 0:
        # function is monotonically decreasing
        # smallest possible value already > 0
        b_opt = 0
    elif obj(min_b) < 0:
        # it is obj(-inf) > 0 always
        logger.info("AcceptanceRateScheme: Numerics limit temperature.")
        b_opt = min_b
    else:
        # perform binary search
        b_opt = sp.optimize.bisect(obj, min_b, 0, maxiter=100000)

    beta_opt = np.exp(b_opt)

    temperature = 1. / beta_opt
    return temperature


class ExpDecayFixedIterScheme(TemperatureScheme):
    """
    The next temperature is set as

    .. math::
        T_j = T_{max}^{(n-j)/n}

    where n denotes the number of populations, and j=1,...,n the iteration.
    This translates to

    .. math::
        T_j = T_{j-1}^{(n-j)/(n-(j-1))}.

    This ensures that a temperature of 1.0 is reached after exactly the
    remaining number of steps.

    So, in both cases the sequence of temperatures follows an exponential
    decay, also known as a geometric progression, or a linear progression
    in log-space.

    Note that the formula is applied anew in each iteration.
    This is advantageous if also other schemes are used s.t. T_{j-1}
    is smaller than by the above.

    Parameters
    ----------

    alpha: float
        Factor by which to reduce the temperature, if `max_nr_populations`
        is infinite.
    """

    def __init__(self):
        pass

    def __call__(self,
                 t: int,
                 get_weighted_distances: Callable[[], pd.DataFrame],
                 get_all_records: Callable[[], List[dict]],
                 max_nr_populations: int,
                 pdf_norm: float,
                 kernel_scale: str,
                 prev_temperature: float,
                 acceptance_rate: float):
        # needs a finite number of iterations
        if max_nr_populations == np.inf:
            raise ValueError(
                "The ExpDecayFixedIterScheme requires a finite "
                "`max_nr_populations`.")

        # needs a starting temperature
        # if not available, return infinite temperature
        if prev_temperature is None:
            return np.inf

        # base temperature
        temp_base = prev_temperature

        # how many steps left?
        t_to_go = max_nr_populations - t

        # compute next temperature according to exponential decay
        temperature = temp_base ** ((t_to_go - 1) / t_to_go)

        return temperature


class ExpDecayFixedRatioScheme(TemperatureScheme):
    """
    The next temperature is chosen as

    .. math::
        T_j = \\alpha \\cdot T_{j-1}.

    Like the :class:`pyabc.epsilon.ExpDecayFixedIterScheme`,
    this yields a geometric progression, however with a fixed ratio,
    irrespective of the number of iterations. If a finite number of
    iterations is specified in ABCSMC, there is no influence on the final
    jump to a temperature of 1.0.

    This is quite similar to the :class:`pyabc.epsilon.DalyScheme`, although
    simpler in implementation. The alpha value here corresponds to a value of
    1 - alpha there.

    Parameters
    ----------
    alpha: float, optional
        The ratio of subsequent temperatures.
    min_rate: float, optional
        A minimum acceptance rate. If this rate has been violated in the
        previous iteration, the alpha value is increased.
    max_rate: float, optional
        Maximum rate to not be exceeded, otherwise the alpha value is
        decreased.
    """
    def __init__(self, alpha: float = 0.5,
                 min_rate: float = 1e-4, max_rate: float = 0.5):
        self.alpha = alpha
        self.min_rate = min_rate
        self.max_rate = max_rate
        self.alphas = {}

    def __call__(self,
                 t: int,
                 get_weighted_distances: Callable[[], pd.DataFrame],
                 get_all_records: Callable[[], List[dict]],
                 max_nr_populations: int,
                 pdf_norm: float,
                 kernel_scale: str,
                 prev_temperature: float,
                 acceptance_rate: float):
        if prev_temperature is None:
            return np.inf

        # previous alpha
        alpha = self.alphas.get(t-1, self.alpha)

        # check if acceptance rate criterion violated
        if acceptance_rate > self.max_rate and t > 1:
            logger.debug("ExpDecayFixedRatioScheme: "
                         "Reacting to high acceptance rate.")
            alpha = max(alpha / 2, alpha - (1 - alpha) * 2)
        if acceptance_rate < self.min_rate:
            logger.debug("ExpDecayFixedRatioScheme: "
                         "Reacting to low acceptance rate.")
            # increase alpha
            alpha = alpha + (1 - alpha) / 2
        # record
        self.alphas[t] = alpha

        # reduce temperature
        temperature = self.alphas[t] * prev_temperature

        return temperature


class PolynomialDecayFixedIterScheme(TemperatureScheme):
    """
    Compute next temperature as pre-last entry in

    >>> np.linspace(1, (temp_base)**(1 / temp_decay_exponent),
    >>>             t_to_go + 1) ** temp_decay_exponent)

    Requires finite `max_nr_populations`.

    Note that this is similar to the
    :class:`pyabc.epsilon.ExpDecayFixedIterScheme`, which is
    indeed the limit for `exponent -> infinity`. For smaller
    exponent, the sequence makes larger steps for low temperatures. This
    can be useful in cases, where lower temperatures (which are usually
    more expensive) can be traversed in few larger steps, however also
    the opposite may be true, i.e. that more steps at low temperatures
    are advantageous.

    Parameters
    ----------
    exponent: float, optional
        The exponent to use in the scheme.
    """

    def __init__(self, exponent: float = 3):
        self.exponent = exponent

    def __call__(self,
                 t: int,
                 get_weighted_distances: Callable[[], pd.DataFrame],
                 get_all_records: Callable[[], List[dict]],
                 max_nr_populations: int,
                 pdf_norm: float,
                 kernel_scale: str,
                 prev_temperature: float,
                 acceptance_rate: float):
        # needs a starting temperature
        # if not available, return infinite temperature
        if prev_temperature is None:
            return np.inf

        # base temperature
        temp_base = prev_temperature

        # check if we can compute a decay step
        if max_nr_populations == np.inf:
            raise ValueError("Can only perform PolynomialDecayScheme step "
                             "with a finite max_nr_populations.")

        # how many steps left?
        t_to_go = max_nr_populations - t

        # compute sequence
        temps = np.linspace(1, (temp_base)**(1 / self.exponent),
                            t_to_go+1) ** self.exponent

        logger.debug(f"Temperatures proposed by polynomial decay method: "
                     f"{temps}.")

        # pre-last step is the next step
        temperature = temps[-2]
        return temperature


class DalyScheme(TemperatureScheme):
    """
    This scheme is loosely based on [#daly2017]_, however note that it does
    not try to replicate it entirely. In particular, the implementation
    of pyABC does not allow the sampling to be stopped when encountering
    too low acceptance rates, such that this can only be done ex-posteriori
    here.

    Parameters
    ----------
    alpha: float, optional
        The ratio by which to decrease the temperature value. More
        specifically, the next temperature is given as
        `(1-alpha) * temperature`.
    min_rate: float, optional
        A minimum acceptance rate. If this rate has been violated in the
        previous iteration, the alpha value is decreased.


    .. [#daly2017] Daly Aidan C., Cooper Jonathan, Gavaghan David J.,
            and Holmes Chris. "Comparing two sequential Monte Carlo samplers
            for exact and approximate Bayesian inference on biological
            models". Journal of The Royal Society Interface, 2017.
    """

    def __init__(self, alpha: float = 0.5, min_rate: float = 1e-4):
        self.alpha = alpha
        self.min_rate = min_rate
        self.k = {}

    def __call__(self,
                 t: int,
                 get_weighted_distances: Callable[[], pd.DataFrame],
                 get_all_records: Callable[[], List[dict]],
                 max_nr_populations: int,
                 pdf_norm: float,
                 kernel_scale: str,
                 prev_temperature: float,
                 acceptance_rate: float):
        # needs a starting temperature
        # if not available, return infinite temperature
        if prev_temperature is None:
            return np.inf

        # base temperature
        temp_base = prev_temperature

        # addressing the std, not the var
        eps_base = np.sqrt(temp_base)

        if not self.k:
            # initial iteration
            self.k[t - 1] = eps_base

        k_base = self.k[t - 1]

        if acceptance_rate < self.min_rate:
            logger.debug("DalyScheme: Reacting to low acceptance rate.")
            # reduce reduction
            k_base = self.alpha * k_base

        self.k[t] = min(k_base, self.alpha * eps_base)
        eps = eps_base - self.k[t]
        temperature = eps**2

        return temperature


class FrielPettittScheme(TemperatureScheme):
    """
    Basically takes linear steps in log-space. See [#vyshemirsky2008]_.

    .. [#vyshemirsky2008] Vyshemirsky, Vladislav, and Mark A. Girolami.
        "Bayesian ranking of biochemical system models."
        Bioinformatics 24.6 (2007): 833-839.
    """

    def __call__(self,
                 t: int,
                 get_weighted_distances: Callable[[], pd.DataFrame],
                 get_all_records: Callable[[], List[dict]],
                 max_nr_populations: int,
                 pdf_norm: float,
                 kernel_scale: str,
                 prev_temperature: float,
                 acceptance_rate: float):
        # needs a starting temperature
        # if not available, return infinite temperature
        if prev_temperature is None:
            return np.inf

        # check if we can compute a decay step
        if max_nr_populations == np.inf:
            raise ValueError("Can only perform FrielPettittScheme step with a "
                             "finite max_nr_populations.")

        # base temperature
        temp_base = prev_temperature
        beta_base = 1. / temp_base

        # time to go
        t_to_go = max_nr_populations - t

        beta = beta_base + ((1. - beta_base) * 1 / t_to_go) ** 2

        temperature = 1. / beta
        return temperature


class EssScheme(TemperatureScheme):
    """
    Try to keep the effective sample size (ESS) constant.

    Parameters
    ----------
    target_relative_ess: float
        Targe relative effective sample size.
    """

    def __init__(self, target_relative_ess: float = 0.8):
        self.target_relative_ess = target_relative_ess

    def __call__(self,
                 t: int,
                 get_weighted_distances: Callable[[], pd.DataFrame],
                 get_all_records: Callable[[], List[dict]],
                 max_nr_populations: int,
                 pdf_norm: float,
                 kernel_scale: str,
                 prev_temperature: float,
                 acceptance_rate: float):
        # execute function (expensive if in calibration)
        df = get_weighted_distances()

        weights = np.array(df['w'], dtype=float)
        pdfs = np.array(df['distance'], dtype=float)

        # compute rescaled posterior densities
        if kernel_scale == SCALE_LIN:
            values = pdfs / pdf_norm
        else:  # kernel_scale == SCALE_LOG
            values = np.exp(pdfs - pdf_norm)

        # to probability mass function (i.e. normalize)
        weights /= np.sum(weights)

        target_ess = len(weights) * self.target_relative_ess

        if prev_temperature is None:
            beta_base = 0.0
        else:
            beta_base = 1. / prev_temperature

        # objective to minimize
        def obj(beta):
            return (_ess(values, weights, beta) - target_ess)**2

        bounds = sp.optimize.Bounds(lb=np.array([beta_base]),
                                    ub=np.array([1.]))
        # TODO make more efficient by providing gradients
        ret = sp.optimize.minimize(
            obj, x0=np.array([0.5 * (1 + beta_base)]),
            bounds=bounds)
        beta = ret.x

        temperature = 1. / beta
        return temperature


def _ess(pdfs, weights, beta):
    """
    Effective sample size (ESS) of importance samples.
    """
    num = np.sum(weights * pdfs**beta)**2
    den = np.sum((weights * pdfs**beta)**2)
    return num / den
