from __future__ import annotations

from statsmodels.compat.pandas import Appender, Substitution
from statsmodels.compat.python import Literal

from collections import defaultdict
from itertools import combinations, product
from types import SimpleNamespace
from typing import (
    Any,
    Dict,
    Hashable,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
    Union,
)
import warnings

import numpy as np
import pandas as pd

import statsmodels.base.wrapper as wrap
from statsmodels.iolib.summary import Summary
from statsmodels.regression.linear_model import OLS
from statsmodels.tools.docstring import Docstring, remove_parameters
from statsmodels.tools.sm_exceptions import SpecificationWarning
from statsmodels.tools.validation import array_like, bool_like, int_like
from statsmodels.tsa.ar_model import (
    AROrderSelectionResults,
    AutoReg,
    AutoRegResults,
    sumofsq,
)
from statsmodels.tsa.arima_process import arma2ma
from statsmodels.tsa.base import tsa_model
from statsmodels.tsa.base.prediction import PredictionResults
from statsmodels.tsa.deterministic import DeterministicProcess
from statsmodels.tsa.tsatools import lagmat


__all__ = [
    "ARDL", "ARDLResults", "ardl_select_order", "ARDLOrderSelectionResults"
]

_ARDLOrder = Union[
    None,
    int,
    Sequence[int],
    Dict[Hashable, Union[None, int, Sequence[int]]],
]

_ArrayLike1D = Union[Sequence[float], np.ndarray, pd.Series]
_ArrayLike2D = Union[Sequence[Sequence[float]], np.ndarray, pd.DataFrame]
_INT_TYPES = (int, np.integer)


def _check_order(order: Union[int, Sequence[int]], causal: bool) -> bool:
    if order is None:
        return True
    if isinstance(order, (int, np.integer)):
        if int(order) < int(causal):
            raise ValueError(
                f"integer orders must be at least {int(causal)} when causal "
                f"is {causal}."
            )
        return True
    for v in order:
        if not isinstance(v, (int, np.integer)):
            raise TypeError(
                "sequence orders must contain non-negative integer values"
            )
    order = [int(v) for v in order]
    if len(set(order)) != len(order) or min(order) < 0:
        raise ValueError(
            "sequence orders must contain distinct non-negative values"
        )
    if int(causal) and min(order) < 1:
        raise ValueError(
            "sequence orders must be strictly positive when causal is True"
        )
    return True


def _format_order(
    exog: _ArrayLike2D, order: _ARDLOrder, causal: bool
) -> Dict[Hashable, List[int]]:
    if exog is None and order in (0, None):
        return {}
    if not isinstance(exog, pd.DataFrame):
        exog = array_like(exog, "exog", ndim=2, maxdim=2)
        keys = list(range(exog.shape[1]))
    else:
        keys = exog.columns
    if order is None:
        exog_order = {k: None for k in keys}
    elif isinstance(order, Mapping):
        exog_order = order
        missing = set(keys).difference(order.keys())
        extra = set(order.keys()).difference(keys)
        if extra:
            msg = (
                "order dictionary contains keys for exogenous "
                "variable(s) that are not contained in exog"
            )
            msg += " Extra keys: "
            msg += ", ".join([str(k) for k in sorted(extra)]) + "."
            raise ValueError(msg)
        if missing:
            msg = (
                "exog contains variables that are missing from the order "
                "dictionary.  Missing keys: "
            )
            msg += ", ".join([str(k) for k in sorted(missing)]) + "."
            warnings.warn(msg, SpecificationWarning)

        for key in exog_order:
            _check_order(exog_order[key], causal)
    elif isinstance(order, _INT_TYPES):
        _check_order(order, causal)
        exog_order = {k: int(order) for k in keys}
    else:
        _check_order(order, causal)
        exog_order = {k: list(order) for k in keys}
    final_order: Dict[Hashable, List[int]] = {}
    for key in exog_order:
        if exog_order[key] is None:
            continue
        if isinstance(exog_order[key], int):
            final_order[key] = list(range(int(causal), exog_order[key] + 1))
        else:
            final_order[key] = [int(lag) for lag in exog_order[key]]

    return final_order


def _format_exog(
    exog: _ArrayLike2D, order: Dict[Hashable, List[int]]
) -> Tuple[Dict[Hashable, np.ndarray], Dict[Hashable, List[str]]]:
    if not order:
        return {}, {}
    max_order = 0
    for val in order.values():
        if val is not None:
            max_order = max(max(val), max_order)
    if not isinstance(exog, pd.DataFrame):
        exog = array_like(exog, "exog", ndim=2, maxdim=2)
    exog_lags = {}
    exog_names = {}
    for key in order:
        if isinstance(exog, np.ndarray):
            col = exog[:, key]
            base = f"x{key}"
        else:
            col = exog[key]
            base = str(key)
        lagged_col = lagmat(col, max_order, original="in")
        lags = order[key]
        exog_lags[key] = lagged_col[:, lags]
        exog_names[key] = [f"{base}.L{lag}" for lag in lags]
    return exog_lags, exog_names


class ARDL(AutoReg):
    r"""
    Autoregressive Distributed Lag (ARDL) Model

    Parameters
    ----------
    endog : array_like
        A 1-d endogenous response variable. The dependent variable.
    lags : {int, list[int]}
        The number of lags to include in the model if an integer or the
        list of lag indices to include.  For example, [1, 4] will only
        include lags 1 and 4 while lags=4 will include lags 1, 2, 3, and 4.
    exog : array_like
        Exogenous variables to include in the model. Either a DataFrame or
        an 2-d array-like structure that can be converted to a NumPy array.
    order : {int, sequence[int], dict}
        If int, uses lags 0, 1, ..., order  for all exog variables. If
        sequence[int], uses the ``order`` for all variables. If a dict,
        applies the lags series by series. If ``exog`` is anything other
        than a DataFrame, the keys are the column index of exog (e.g., 0,
        1, ...). If a DataFrame, keys are column names.
    fixed : array_like
        Additional fixed regressors that are not lagged.
    causal : bool, optional
        Whether to include lag 0 of exog variables.  If True, only includes
        lags 1, 2, ...
    trend : {'n', 'c', 't', 'ct'}, optional
        The trend to include in the model:

        * 'n' - No trend.
        * 'c' - Constant only.
        * 't' - Time trend only.
        * 'ct' - Constant and time trend.

        The default is 'c'.

    seasonal : bool, optional
        Flag indicating whether to include seasonal dummies in the model. If
        seasonal is True and trend includes 'c', then the first period
        is excluded from the seasonal terms.
    deterministic : DeterministicProcess, optional
        A deterministic process.  If provided, trend and seasonal are ignored.
        A warning is raised if trend is not "n" and seasonal is not False.
    hold_back : {None, int}, optional
        Initial observations to exclude from the estimation sample.  If None,
        then hold_back is equal to the maximum lag in the model.  Set to a
        non-zero value to produce comparable models with different lag
        length.  For example, to compare the fit of a model with lags=3 and
        lags=1, set hold_back=3 which ensures that both models are estimated
        using observations 3,...,nobs. hold_back must be >= the maximum lag in
        the model.
    period : {None, int}, optional
        The period of the data. Only used if seasonal is True. This parameter
        can be omitted if using a pandas object for endog that contains a
        recognized frequency.
    missing : {"none", "drop", "raise"}, optional
        Available options are 'none', 'drop', and 'raise'. If 'none', no nan
        checking is done. If 'drop', any observations with nans are dropped.
        If 'raise', an error is raised. Default is 'none'.

    Notes
    -----
    The full specification of an ARDL is

    .. math ::

       Y_t = \delta_0 + \delta_1 t + \delta_2 t^2
             + \sum_{i=1}^{s-1} \gamma_i I_{[(\mod(t,s) + 1) = i]}
             + \sum_{j=1}^p \phi_j Y_{t-j}
             + \sum_{l=1}^k \sum_{m=0}^{o_l} \beta_{l,m} X_{l, t-m}
             + Z_t \lambda
             + \epsilon_t

    where :math:`\delta_\bullet` capture trends, :math:`\gamma_\bullet`
    capture seasonal shifts, s is the period of the seasonality, p is the
    lag length of the endogenous variable, k is the number of exogenous
    variables :math:`X_{l}`, :math:`o_l` is included the lag length of
    :math:`X_{l}`, :math:`Z_t` are ``r`` included fixed regressors and
    :math:`\epsilon_t` is a white noise shock. If ``causal`` is ``True``,
    then the 0-th lag of the exogenous variables is not included and the
    sum starts at ``m=1``.

    See Also
    --------
    statsmodels.tsa.ar_model.AutoReg
        Autoregressive model estimation with optional exogenous regressors
    statsmodels.tsa.statespace.sarimax.SARIMAX
        Seasonal ARIMA model estimation with optional exogenous regressors
    statsmodels.tsa.arima.model.ARIMA
        ARIMA model estimation

    Examples
    --------
    >>> from statsmodels.tsa.api import ARDL
    >>> from statsmodels.datasets import danish_data
    >>> data = danish_data.load_pandas().data
    >>> lrm = data.lrm
    >>> exog = data[["lry", "ibo", "ide"]]

    A biasic model where all variables have 3 lags included

    >>> ARDL(data.lrm, 3, data[["lry", "ibo", "ide"]], 3)

    A dictionary can be used to pass custom lag orders

    >>> ARDL(data.lrm, [1, 3], exog, {"lry": 1, "ibo": 3, "ide": 2})

    Setting causal removes the 0-th lag from the exogenous variables

    >>> exog_lags = {"lry": 1, "ibo": 3, "ide": 2}
    >>> ARDL(data.lrm, [1, 3], exog, exog_lags, causal=True)

    A dictionary can also be used to pass specific lags to include.
    Sequences hold the specific lags to include, while integers are expanded
    to include [0, 1, ..., lag]. If causal is False, then the 0-th lag is
    excluded.

    >>> ARDL(lrm, [1, 3], exog, {"lry": [0, 1], "ibo": [0, 1, 3], "ide": 2})

    When using NumPy arrays, the dictionary keys are the column index.

    >>> import numpy as np
    >>> lrma = np.asarray(lrm)
    >>> exoga = np.asarray(exog)
    >>> ARDL(lrma, 3, exoga, {0: [0, 1], 1: [0, 1, 3], 2: 2})
    """

    def __init__(
        self,
        endog: Union[Sequence[float], pd.Series, _ArrayLike2D],
        lags: Union[None, int, Sequence[int]],
        exog: Optional[_ArrayLike2D] = None,
        order: _ARDLOrder = 0,
        trend: Literal["n", "c", "ct", "ctt"] = "c",
        *,
        fixed: Optional[_ArrayLike2D] = None,
        causal: bool = False,
        seasonal: bool = False,
        deterministic: Optional[DeterministicProcess] = None,
        hold_back: Optional[int] = None,
        period: Optional[int] = None,
        missing: Literal["none", "drop", "raise"] = "none",
    ) -> None:
        super().__init__(
            endog,
            lags,
            trend=trend,
            seasonal=seasonal,
            exog=exog,
            hold_back=hold_back,
            period=period,
            missing=missing,
            deterministic=deterministic,
            old_names=False,
        )
        # Reset hold back which was set in AutoReg.__init__
        self._hold_back = int_like(hold_back, "hold_back", optional=True)
        self._causal = bool_like(causal, "causal", strict=True)
        if fixed is not None:
            fixed_arr = array_like(fixed, "fixed", ndim=2, maxdim=2)
            if fixed_arr.shape[0] != self.data.endog.shape[0] or not np.all(
                np.isfinite(fixed_arr)
            ):
                raise ValueError(
                    "fixed must be an (nobs, m) array where nobs matches the "
                    "number of observations in the endog variable, and all"
                    "values must be finite"
                )
            if isinstance(fixed, pd.DataFrame):
                self._fixed_names = list(fixed.columns)
            else:
                self._fixed_names = [
                    f"z.{i}" for i in range(fixed_arr.shape[1])
                ]
            self._fixed = fixed_arr
        else:
            self._fixed = np.empty((self.data.endog.shape[0], 0))
            self._fixed_names = []

        self._initialize_model(lags, order)
        self._causal = True
        if self._order:
            min_lags = [min(val) for val in self._order.values()]
            self._causal = min(min_lags) > 0

    @property
    def ar_lags(self) -> Optional[List[int]]:
        """The autoregressive lags included in the model"""
        return None if not self._lags else self._lags

    @property
    def exog_lags(self) -> Dict[Hashable, List[int]]:
        """The lags of exogenous variables included in the model"""
        return self._order

    @property
    def ardl_order(self) -> Tuple[int, int]:
        """The order of the ARDL(p,q)"""
        ar_order = 0 if not self._lags else max(self._lags)
        dl_order = -1
        for lags in self._order.values():
            if lags is not None:
                dl_order = max(dl_order, max(lags))
        dl_order = None if dl_order < 0 else dl_order
        return ar_order, dl_order

    def _setup_regressors(self):
        # Place holder to let AutoReg init complete
        self._y = np.empty((self.endog.shape[0] - self._hold_back, 0))

    def _initialize_model(
        self, lags: Union[None, int, Sequence[int]], order: _ARDLOrder
    ) -> None:
        # TODO: Missing adjustment
        lags = 0 if lags is None else lags
        if isinstance(lags, _INT_TYPES):
            lags = list(range(1, int(lags) + 1))
        else:
            lags = list([int(lag) for lag in lags])
        self._lags = lags
        self._maxlag = max(lags) if lags else 0
        self._endog_reg, self._endog = lagmat(
            self.data.endog, self._maxlag, original="sep"
        )
        if self._endog_reg.shape[1] != len(self._lags):
            lag_locs = [l - 1 for l in self._lags]
            self._endog_reg = self._endog_reg[:, lag_locs]
        y_name = self.data.ynames
        self._endog_lag_names = [f"{y_name}.L{i}" for i in lags]
        self._order = _format_order(self.data.orig_exog, order, self._causal)
        self._exog, self._exog_var_names = _format_exog(
            self.data.orig_exog, self._order
        )
        exog_maxlag = 0
        for val in self._order.values():
            exog_maxlag = max(exog_maxlag, max(val) if val is not None else 0)
        self._maxlag = max(self._maxlag, exog_maxlag)
        self._deterministic_reg = self._deterministics.in_sample()
        self._blocks = {
            "endog": self._endog_reg,
            "exog": self._exog,
            "deterministic": self._deterministic_reg,
            "fixed": self._fixed,
        }
        self._names = {
            "endog": self._endog_lag_names,
            "exog": self._exog_var_names,
            "deterministic": self._deterministic_reg.columns,
            "fixed": self._fixed_names,
        }
        self._exog_names = list(self._deterministic_reg.columns)
        self._exog_names += self._endog_lag_names[:]
        for key in self._exog_var_names:
            self._exog_names += self._exog_var_names[key]
        self._exog_names += self._fixed_names
        self.data.param_names = self.data.xnames = self._exog_names
        x = [self._deterministic_reg, self._endog_reg]
        x += [ex for ex in self._exog.values()] + [self._fixed]
        self._x = np.column_stack(x)
        if self._hold_back is None:
            self._hold_back = self._maxlag
        if self._hold_back < self._maxlag:
            raise ValueError(
                "hold_back must be >= the maximum lag of the endog and exog "
                "variables"
            )
        self._x = self._x[self._hold_back :]
        if self._x.shape[1] > self._x.shape[0]:
            raise ValueError(
                f"The number of regressors ({self._x.shape[1]}) including "
                "deterministics, lags of the endog, lags of the exogenous, "
                "and fixed regressors is larer than the sample available "
                f"for estimation ({self._x.shape[0]})."
            )
        self._y = self.data.endog[self._hold_back :]

    def fit(
        self,
        cov_type: str = "nonrobust",
        cov_kwds: Dict[str, Any] = None,
        use_t: bool = False,
    ) -> ARDLResults:
        """
        Estimate the model parameters.

        Parameters
        ----------
        cov_type : str
            The covariance estimator to use. The most common choices are listed
            below.  Supports all covariance estimators that are available
            in ``OLS.fit``.

            * 'nonrobust' - The class OLS covariance estimator that assumes
              homoskedasticity.
            * 'HC0', 'HC1', 'HC2', 'HC3' - Variants of White's
              (or Eiker-Huber-White) covariance estimator. `HC0` is the
              standard implementation.  The other make corrections to improve
              the finite sample performance of the heteroskedasticity robust
              covariance estimator.
            * 'HAC' - Heteroskedasticity-autocorrelation robust covariance
              estimation. Supports cov_kwds.

              - `maxlags` integer (required) : number of lags to use.
              - `kernel` callable or str (optional) : kernel
                  currently available kernels are ['bartlett', 'uniform'],
                  default is Bartlett.
              - `use_correction` bool (optional) : If true, use small sample
                  correction.
        cov_kwds : dict, optional
            A dictionary of keyword arguments to pass to the covariance
            estimator. `nonrobust` and `HC#` do not support cov_kwds.
        use_t : bool, optional
            A flag indicating that inference should use the Student's t
            distribution that accounts for model degree of freedom.  If False,
            uses the normal distribution. If None, defers the choice to
            the cov_type. It also removes degree of freedom corrections from
            the covariance estimator when cov_type is 'nonrobust'.

        Returns
        -------
        ARDLResults
            Estimation results.

        See Also
        --------
        statsmodels.tsa.ar_model.AutoReg
            Ordinary Least Squares estimation.
        statsmodels.regression.linear_model.OLS
            Ordinary Least Squares estimation.
        statsmodels.regression.linear_model.RegressionResults
            See ``get_robustcov_results`` for a detailed list of available
            covariance estimators and options.

        Notes
        -----
        Use ``OLS`` to estimate model parameters and to estimate parameter
        covariance.
        """
        if self._x.shape[1] == 0:
            res = ARDLResults(
                self, np.empty((0,)), np.empty((0, 0)), np.empty((0, 0))
            )
            return ARDLResultsWrapper(res)
        ols_mod = OLS(self._y, self._x)
        ols_res = ols_mod.fit(
            cov_type=cov_type, cov_kwds=cov_kwds, use_t=use_t
        )
        cov_params = ols_res.cov_params()
        use_t = ols_res.use_t
        if cov_type == "nonrobust" and not use_t:
            nobs = self._y.shape[0]
            k = self._x.shape[1]
            scale = nobs / (nobs - k)
            cov_params /= scale

        res = ARDLResults(
            self, ols_res.params, cov_params, ols_res.normalized_cov_params
        )
        return ARDLResultsWrapper(res)

    def _forecasting_x(
        self,
        start: int,
        end: int,
        num_oos: int,
        exog: Optional[_ArrayLike2D],
        exog_oos: Optional[_ArrayLike2D],
        fixed: Optional[_ArrayLike2D],
        fixed_oos: Optional[_ArrayLike2D],
    ) -> np.ndarray:
        def pad_x(x: np.ndarray, pad: int):
            if pad == 0:
                return x
            k = x.shape[1]
            return np.vstack([np.full((pad, k), np.nan), x])

        pad = 0 if start >= self._hold_back else self._hold_back - start
        # Shortcut if all in-sample and no new data

        if (end + 1) < self.endog.shape[0] and exog is None and fixed is None:
            adjusted_start = max(start - self._hold_back, 0)
            return pad_x(
                self._x[adjusted_start : end + 1 - self._hold_back], pad
            )

        # If anything changed, rebuild x array
        exog = self.data.exog if exog is None else np.asarray(exog)
        if exog_oos is not None:
            exog = np.vstack([exog, np.asarray(exog_oos)[:num_oos]])
        fixed = self._fixed if fixed is None else np.asarray(fixed)
        if fixed_oos is not None:
            fixed = np.vstack([fixed, np.asarray(fixed_oos)[:num_oos]])
        det = self._deterministics.in_sample()
        if num_oos:
            oos_det = self._deterministics.out_of_sample(num_oos)
            det = pd.concat([det, oos_det], 0)
        endog = self.data.endog
        if num_oos:
            endog = np.hstack([endog, np.full(num_oos, np.nan)])
        x = [det]
        if self._lags:
            endog_reg = lagmat(endog, max(self._lags), original="ex")
            x.append(endog_reg[:, [lag - 1 for lag in self._lags]])
        if self.ardl_order[1] is not None:
            if isinstance(self.data.orig_exog, pd.DataFrame):
                exog = pd.DataFrame(exog, columns=self.data.orig_exog.columns)
            exog, _ = _format_exog(exog, self._order)
            x.extend([np.asarray(arr) for arr in exog.values()])
        if fixed.shape[1] > 0:
            x.append(fixed)
        _x = np.column_stack(x)
        _x[: self._hold_back] = np.nan
        return _x[start:]

    def predict(
        self,
        params,
        start=None,
        end=None,
        dynamic=False,
        exog=None,
        exog_oos=None,
        fixed=None,
        fixed_oos=None,
    ):
        """
        In-sample prediction and out-of-sample forecasting.

        Parameters
        ----------
        params : array_like
            The fitted model parameters.
        start : int, str, or datetime, optional
            Zero-indexed observation number at which to start forecasting,
            i.e., the first forecast is start. Can also be a date string to
            parse or a datetime type. Default is the the zeroth observation.
        end : int, str, or datetime, optional
            Zero-indexed observation number at which to end forecasting, i.e.,
            the last forecast is end. Can also be a date string to
            parse or a datetime type. However, if the dates index does not
            have a fixed frequency, end must be an integer index if you
            want out-of-sample prediction. Default is the last observation in
            the sample. Unlike standard python slices, end is inclusive so
            that all the predictions [start, start+1, ..., end-1, end] are
            returned.
        dynamic : {bool, int, str, datetime, Timestamp}, optional
            Integer offset relative to `start` at which to begin dynamic
            prediction. Prior to this observation, true endogenous values
            will be used for prediction; starting with this observation and
            continuing through the end of prediction, forecasted endogenous
            values will be used instead. Datetime-like objects are not
            interpreted as offsets. They are instead used to find the index
            location of `dynamic` which is then used to to compute the offset.
        exog : array_like
            A replacement exogenous array.  Must have the same shape as the
            exogenous data array used when the model was created.
        exog_oos : array_like
            An array containing out-of-sample values of the exogenous
            variables. Must have the same number of columns as the exog
            used when the model was created, and at least as many rows as
            the number of out-of-sample forecasts.
        fixed : array_like
            A replacement fixed array.  Must have the same shape as the
            fixed data array used when the model was created.
        fixed_oos : array_like
            An array containing out-of-sample values of the fixed variables.
            Must have the same number of columns as the fixed used when the
            model was created, and at least as many rows as the number of
            out-of-sample forecasts.

        Returns
        -------
        predictions : {ndarray, Series}
            Array of out of in-sample predictions and / or out-of-sample
            forecasts.
        """
        params, exog, exog_oos, start, end, num_oos = self._prepare_prediction(
            params, exog, exog_oos, start, end
        )

        def check_exog(arr, name, orig, exact):
            if isinstance(orig, pd.DataFrame):
                if not isinstance(arr, pd.DataFrame):
                    raise TypeError(
                        f"{name} must be a DataFrame when the original exog "
                        f"was a DataFrame"
                    )
                if sorted(arr.columns) != sorted(self.data.orig_exog.columns):
                    raise ValueError(
                        f"{name} must have the same columns as the original "
                        f"exog"
                    )
            else:
                arr = array_like(arr, name, ndim=2, optional=False)
            if arr.ndim != 2 or arr.shape[1] != orig.shape[1]:
                raise ValueError(
                    f"{name} must have the same number of columns as the "
                    f"original data, {orig.shape[1]}"
                )
            if exact and arr.shape[0] != orig.shape[0]:
                raise ValueError(
                    f"{name} must have the same number of rows as the "
                    f"original data ({n})."
                )
            return arr

        n = self.data.endog.shape[0]
        if exog is not None:
            exog = check_exog(exog, "exog", self.data.orig_exog, True)
        if exog_oos is not None:
            exog_oos = check_exog(
                exog_oos, "exog_oos", self.data.orig_exog, False
            )
        if fixed is not None:
            fixed = check_exog(fixed, "fixed", self._fixed, True)
        if fixed_oos is not None:
            fixed_oos = check_exog(
                np.asarray(fixed_oos), "fixed_oos", self._fixed, False
            )
        # The maximum number of 1-step predictions that can be made,
        # which depends on the model and lags
        if self._fixed.shape[1] or not self._causal:
            max_1step = 0
        else:
            max_1step = np.inf if not self._lags else min(self._lags)
            if self._order:
                min_exog = min([min(v) for v in self._order.values()])
                max_1step = min(max_1step, min_exog)
        if num_oos > max_1step:
            if self._order and exog_oos is None:
                raise ValueError(
                    "exog_oos must be provided when out-of-sample "
                    "observations require values of the exog not in the "
                    "original sample"
                )
            elif self._order and (exog_oos.shape[0] + max_1step) < num_oos:
                raise ValueError(
                    f"exog_oos must have at least {num_oos - max_1step} "
                    f"observations to produce {num_oos} forecasts based on "
                    f"the model specification."
                )

            if self._fixed.shape[1] and fixed_oos is None:
                raise ValueError(
                    "fixed_oos must be provided when predicting "
                    "out-of-sample observations"
                )
            elif self._fixed.shape[1] and fixed_oos.shape[0] < num_oos:
                raise ValueError(
                    f"fixed_oos must have at least {num_oos} observations "
                    f"to produce {num_oos} forecasts."
                )
        # Extend exog_oos if fcast is valid for horizon but no exog_oos given
        if self.exog is not None and exog_oos is None and num_oos:
            exog_oos = np.full((num_oos, self.exog.shape[1]), np.nan)
            if isinstance(self.data.orig_exog, pd.DataFrame):
                exog_oos = pd.DataFrame(
                    exog_oos, columns=self.data.orig_exog.columns
                )
        x = self._forecasting_x(
            start, end, num_oos, exog, exog_oos, fixed, fixed_oos
        )
        if dynamic is False:
            dynamic_start = end + 1 - start
        else:
            dynamic_step = self._parse_dynamic(dynamic, start)
            dynamic_start = dynamic_step
            if start < self._hold_back:
                dynamic_start = max(dynamic_start, self._hold_back - start)

        fcasts = np.full(x.shape[0], np.nan)
        fcasts[:dynamic_start] = x[:dynamic_start] @ params
        offset = self._deterministic_reg.shape[1]
        for i in range(dynamic_start, fcasts.shape[0]):
            for j, lag in enumerate(self._lags):
                loc = i - lag
                if loc >= dynamic_start:
                    val = fcasts[loc]
                else:
                    # Actual data
                    val = self.endog[start + loc]
                x[i, offset + j] = val
            fcasts[i] = x[i] @ params
        return self._wrap_prediction(fcasts, start, end + 1 + num_oos, 0)

    @classmethod
    def from_formula(
        cls,
        data: pd.DataFrame,
        formula: str,
        *,
        trend: Literal["n", "c", "ct", "ctt"] = "n",
        seasonal: bool = False,
        deterministic: Optional[DeterministicProcess] = None,
        hold_back: Optional[int] = None,
        period: Optional[int] = None,
        missing: Literal["none", "raise"] = "none",
    ) -> ARDL:
        raise NotImplementedError("formulas have not been implemented")


doc = Docstring(ARDL.predict.__doc__)
_predict_params = doc.extract_parameters(
    ["start", "end", "dynamic", "exog", "exog_oos", "fixed", "fixed_oos"], 8
)


class ARDLResults(AutoRegResults):
    """
    Class to hold results from fitting an ARDL model.

    Parameters
    ----------
    model : ARDL
        Reference to the model that is fit.
    params : ndarray
        The fitted parameters from the AR Model.
    cov_params : ndarray
        The estimated covariance matrix of the model parameters.
    normalized_cov_params : ndarray
        The array inv(dot(x.T,x)) where x contains the regressors in the
        model.
    scale : float, optional
        An estimate of the scale of the model.
    """

    _cache = {}  # for scale setter

    def __init__(
        self, model, params, cov_params, normalized_cov_params=None, scale=1.0
    ):
        super().__init__(model, params, normalized_cov_params, scale)
        self._cache = {}
        self._params = params
        self._nobs = model.nobs
        self._n_totobs = model.endog.shape[0]
        self._df_model = model.df_model
        self._ar_lags = model.ar_lags
        self._max_lag = 0
        if self._ar_lags:
            self._max_lag = max(self._ar_lags)
        self._hold_back = self.model.hold_back
        self.cov_params_default = cov_params

    @Appender(remove_parameters(ARDL.predict.__doc__, "params"))
    def predict(
        self,
        start=None,
        end=None,
        dynamic=False,
        exog=None,
        exog_oos=None,
        fixed=None,
        fixed_oos=None,
    ):
        return self.model.predict(
            self._params,
            start=start,
            end=end,
            dynamic=dynamic,
            exog=exog,
            exog_oos=exog_oos,
            fixed=fixed,
            fixed_oos=fixed_oos,
        )

    def forecast(self, steps=1, exog=None, fixed=None):
        """
        Out-of-sample forecasts

        Parameters
        ----------
        steps : {int, str, datetime}, default 1
            If an integer, the number of steps to forecast from the end of the
            sample. Can also be a date string to parse or a datetime type.
            However, if the dates index does not have a fixed frequency,
            steps must be an integer.
        exog : array_like, optional
            Exogenous values to use out-of-sample. Must have same number of
            columns as original exog data and at least `steps` rows
        fixed : array_like, optional
            Fixed values to use out-of-sample. Must have same number of
            columns as original fixed data and at least `steps` rows

        Returns
        -------
        array_like
            Array of out of in-sample predictions and / or out-of-sample
            forecasts.

        See Also
        --------
        ARDLResults.predict
            In- and out-of-sample predictions
        ARDLResults.get_prediction
            In- and out-of-sample predictions and confidence intervals
        """
        start = self.model.data.orig_endog.shape[0]
        if isinstance(steps, (int, np.integer)):
            end = start + steps - 1
        else:
            end = steps
        return self.predict(
            start=start, end=end, dynamic=False, exog_oos=exog, fixed_oos=fixed
        )

    def _lag_repr(self):
        """Returns poly repr of an AR, (1  -phi1 L -phi2 L^2-...)"""
        ar_lags = self._ar_lags if self._ar_lags is not None else []
        k_ar = len(ar_lags)
        ar_params = np.zeros(self._max_lag + 1)
        ar_params[0] = 1
        offset = self.model._deterministic_reg.shape[1]
        params = self._params[offset : offset + k_ar]
        for i, lag in enumerate(ar_lags):
            ar_params[lag] = -params[i]
        return ar_params

    def get_prediction(
        self,
        start=None,
        end=None,
        dynamic=False,
        exog=None,
        exog_oos=None,
        fixed=None,
        fixed_oos=None,
    ):
        """
        Predictions and prediction intervals

        Parameters
        ----------
        start : int, str, or datetime, optional
            Zero-indexed observation number at which to start forecasting,
            i.e., the first forecast is start. Can also be a date string to
            parse or a datetime type. Default is the the zeroth observation.
        end : int, str, or datetime, optional
            Zero-indexed observation number at which to end forecasting, i.e.,
            the last forecast is end. Can also be a date string to
            parse or a datetime type. However, if the dates index does not
            have a fixed frequency, end must be an integer index if you
            want out-of-sample prediction. Default is the last observation in
            the sample. Unlike standard python slices, end is inclusive so
            that all the predictions [start, start+1, ..., end-1, end] are
            returned.
        dynamic : {bool, int, str, datetime, Timestamp}, optional
            Integer offset relative to `start` at which to begin dynamic
            prediction. Prior to this observation, true endogenous values
            will be used for prediction; starting with this observation and
            continuing through the end of prediction, forecasted endogenous
            values will be used instead. Datetime-like objects are not
            interpreted as offsets. They are instead used to find the index
            location of `dynamic` which is then used to to compute the offset.
        exog : array_like
            A replacement exogenous array.  Must have the same shape as the
            exogenous data array used when the model was created.
        exog_oos : array_like
            An array containing out-of-sample values of the exogenous variable.
            Must has the same number of columns as the exog used when the
            model was created, and at least as many rows as the number of
            out-of-sample forecasts.
        fixed : array_like
            A replacement fixed array.  Must have the same shape as the
            fixed data array used when the model was created.
        fixed_oos : array_like
            An array containing out-of-sample values of the fixed variables.
            Must have the same number of columns as the fixed used when the
            model was created, and at least as many rows as the number of
            out-of-sample forecasts.

        Returns
        -------
        PredictionResults
            Prediction results with mean and prediction intervals
        """
        mean = self.predict(
            start=start,
            end=end,
            dynamic=dynamic,
            exog=exog,
            exog_oos=exog_oos,
            fixed=fixed,
            fixed_oos=fixed_oos,
        )
        mean_var = np.full_like(mean, fill_value=self.sigma2)
        mean_var[np.isnan(mean)] = np.nan
        start = 0 if start is None else start
        end = self.model._index[-1] if end is None else end
        _, _, oos, _ = self.model._get_prediction_index(start, end)
        if oos > 0:
            ar_params = self._lag_repr()
            ma = arma2ma(ar_params, np.ones(1), lags=oos)
            mean_var[-oos:] = self.sigma2 * np.cumsum(ma ** 2)
        if isinstance(mean, pd.Series):
            mean_var = pd.Series(mean_var, index=mean.index)

        return PredictionResults(mean, mean_var)

    @Substitution(predict_params=_predict_params)
    def plot_predict(
        self,
        start=None,
        end=None,
        dynamic=False,
        exog=None,
        exog_oos=None,
        fixed=None,
        fixed_oos=None,
        alpha=0.05,
        in_sample=True,
        fig=None,
        figsize=None,
    ):
        """
        Plot in- and out-of-sample predictions

        Parameters
        ----------
%(predict_params)s
        alpha : {float, None}
            The tail probability not covered by the confidence interval. Must
            be in (0, 1). Confidence interval is constructed assuming normally
            distributed shocks. If None, figure will not show the confidence
            interval.
        in_sample : bool
            Flag indicating whether to include the in-sample period in the
            plot.
        fig : Figure
            An existing figure handle. If not provided, a new figure is
            created.
        figsize: tuple[float, float]
            Tuple containing the figure size values.

        Returns
        -------
        Figure
            Figure handle containing the plot.
        """
        predictions = self.get_prediction(
            start=start,
            end=end,
            dynamic=dynamic,
            exog=exog,
            exog_oos=exog_oos,
            fixed=fixed,
            fixed_oos=fixed_oos,
        )
        return self._plot_predictions(
            predictions, start, end, alpha, in_sample, fig, figsize
        )

    def summary(self, alpha=0.05):
        """
        Summarize the Model

        Parameters
        ----------
        alpha : float, optional
            Significance level for the confidence intervals.

        Returns
        -------
        smry : Summary instance
            This holds the summary table and text, which can be printed or
            converted to various output formats.

        See Also
        --------
        statsmodels.iolib.summary.Summary
        """
        model = self.model

        title = model.__class__.__name__ + " Model Results"
        method = "Conditional MLE"
        # get sample
        start = self._hold_back
        if self.data.dates is not None:
            dates = self.data.dates
            sample = [dates[start].strftime("%m-%d-%Y")]
            sample += ["- " + dates[-1].strftime("%m-%d-%Y")]
        else:
            sample = [str(start), str(len(self.data.orig_endog))]
        model = self.model.__class__.__name__ + str(self.model.ardl_order)
        if self.model.seasonal:
            model = "Seas. " + model

        order = "({0})".format(self._max_lag)
        dep_name = str(self.model.endog_names)
        top_left = [
            ("Dep. Variable:", [dep_name]),
            ("Model:", [model + order]),
            ("Method:", [method]),
            ("Date:", None),
            ("Time:", None),
            ("Sample:", [sample[0]]),
            ("", [sample[1]]),
        ]

        top_right = [
            ("No. Observations:", [str(len(self.model.endog))]),
            ("Log Likelihood", ["%#5.3f" % self.llf]),
            ("S.D. of innovations", ["%#5.3f" % self.sigma2 ** 0.5]),
            ("AIC", ["%#5.3f" % self.aic]),
            ("BIC", ["%#5.3f" % self.bic]),
            ("HQIC", ["%#5.3f" % self.hqic]),
        ]

        smry = Summary()
        smry.add_table_2cols(
            self, gleft=top_left, gright=top_right, title=title
        )
        smry.add_table_params(self, alpha=alpha, use_t=False)

        return smry


class ARDLResultsWrapper(wrap.ResultsWrapper):
    _attrs = {}
    _wrap_attrs = wrap.union_dicts(
        tsa_model.TimeSeriesResultsWrapper._wrap_attrs, _attrs
    )
    _methods = {}
    _wrap_methods = wrap.union_dicts(
        tsa_model.TimeSeriesResultsWrapper._wrap_methods, _methods
    )


wrap.populate_wrapper(ARDLResultsWrapper, ARDLResults)


class ARDLOrderSelectionResults(AROrderSelectionResults):
    """
    Results from an ARDL order selection

    Contains the information criteria for all fitted model orders.
    """

    def __init__(self, model, ics, trend, seasonal, period):
        _ics = (((0,), (0, 0, 0)),)
        super().__init__(model, _ics, trend, seasonal, period)

        def _to_dict(d):
            return d[0], dict(d[1:])

        self._aic = pd.Series(
            {v[0]: _to_dict(k) for k, v in ics.items()}, dtype=object
        )
        self._aic.index.name = self._aic.name = "AIC"
        self._aic = self._aic.sort_index()

        self._bic = pd.Series(
            {v[1]: _to_dict(k) for k, v in ics.items()}, dtype=object
        )
        self._bic.index.name = self._bic.name = "BIC"
        self._bic = self._bic.sort_index()

        self._hqic = pd.Series(
            {v[2]: _to_dict(k) for k, v in ics.items()}, dtype=object
        )
        self._hqic.index.name = self._hqic.name = "HQIC"
        self._hqic = self._hqic.sort_index()

    @property
    def exog_lags(self) -> Dict[Hashable, List[int]]:
        """The lags of exogenous variables in the selected model"""
        return self._model.exog_lags


def ardl_select_order(
    endog: Union[Sequence[float], pd.Series, _ArrayLike2D],
    maxlag: int,
    exog: _ArrayLike2D,
    maxorder: Union[int, Dict[Hashable, int]],
    trend: Literal["n", "c", "ct", "ctt"] = "c",
    *,
    fixed: Optional[_ArrayLike2D] = None,
    causal: bool = False,
    ic: Literal["aic", "bic"] = "bic",
    glob: bool = False,
    seasonal: bool = False,
    deterministic: Optional[DeterministicProcess] = None,
    hold_back: Optional[int] = None,
    period: Optional[int] = None,
    missing: Literal["none", "raise"] = "none",
) -> ARDLOrderSelectionResults:
    r"""
    ARDL order selection

    Parameters
    ----------
    endog : array_like
        A 1-d endogenous response variable. The dependent variable.
    maxlag : int
        The maximum lag to consider for the endogenous variable.
    exog : array_like
        Exogenous variables to include in the model. Either a DataFrame or
        an 2-d array-like structure that can be converted to a NumPy array.
    maxorder : {int, dict}
        If int, sets a common max lag length for all exog variables. If
        a dict, then sets individual lag length. They keys are column names
        if exog is a DataFrame or column indices otherwise.
    trend : {'n', 'c', 't', 'ct'}, optional
        The trend to include in the model:

        * 'n' - No trend.
        * 'c' - Constant only.
        * 't' - Time trend only.
        * 'ct' - Constant and time trend.

        The default is 'c'.
    fixed : array_like
        Additional fixed regressors that are not lagged.
    causal : bool, optional
        Whether to include lag 0 of exog variables.  If True, only includes
        lags 1, 2, ...
    ic : {"aic", "bic", "hqic"}
        The information criterion to use in model selection.
    glob : bool
        Whether to consider all possible submodels of the largest model
        or only if smaller order lags must be included if larger order
        lags are.  If ``True``, the number of model considered is of the
        order 2**(maxlag + k * maxorder) assuming maxorder is an int. This
        can be very large unless k and maxorder are bot relatively small.
        If False, the number of model considered is of the order
        maxlag*maxorder**k which may also be substantial when k and maxorder
        are large.
    seasonal : bool, optional
        Flag indicating whether to include seasonal dummies in the model. If
        seasonal is True and trend includes 'c', then the first period
        is excluded from the seasonal terms.
    deterministic : DeterministicProcess, optional
        A deterministic process.  If provided, trend and seasonal are ignored.
        A warning is raised if trend is not "n" and seasonal is not False.
    hold_back : {None, int}, optional
        Initial observations to exclude from the estimation sample.  If None,
        then hold_back is equal to the maximum lag in the model.  Set to a
        non-zero value to produce comparable models with different lag
        length.  For example, to compare the fit of a model with lags=3 and
        lags=1, set hold_back=3 which ensures that both models are estimated
        using observations 3,...,nobs. hold_back must be >= the maximum lag in
        the model.
    period : {None, int}, optional
        The period of the data. Only used if seasonal is True. This parameter
        can be omitted if using a pandas object for endog that contains a
        recognized frequency.
    missing : {"none", "drop", "raise"}, optional
        Available options are 'none', 'drop', and 'raise'. If 'none', no nan
        checking is done. If 'drop', any observations with nans are dropped.
        If 'raise', an error is raised. Default is 'none'.

    Returns
    -------
    ARDLSelectionResults
        A results holder containing the selected model and the complete set
        of information criteria for all models fit.
    """

    def compute_ics(y, x, df):
        if x.shape[1]:
            resid = y - x @ np.linalg.lstsq(x, y, rcond=None)[0]
        else:
            resid = y
        nobs = resid.shape[0]
        sigma2 = sumofsq(resid)
        res = SimpleNamespace(
            nobs=nobs, df_model=df + x.shape[1], sigma2=sigma2
        )

        aic = ARDLResults.aic.func(res)
        bic = ARDLResults.bic.func(res)
        hqic = ARDLResults.hqic.func(res)

        return aic, bic, hqic

    base = ARDL(
        endog,
        maxlag,
        exog,
        maxorder,
        trend,
        fixed=fixed,
        causal=causal,
        seasonal=seasonal,
        deterministic=deterministic,
        hold_back=hold_back,
        period=period,
        missing=missing,
    )
    hold_back = base.hold_back
    blocks = base._blocks
    always = np.column_stack([blocks["deterministic"], blocks["fixed"]])
    always = always[hold_back:]
    select = []
    iter_orders = []
    select.append(blocks["endog"][hold_back:])
    iter_orders.append(list(range(blocks["endog"].shape[1] + 1)))
    var_names = []
    for var in blocks["exog"]:
        block = blocks["exog"][var][hold_back:]
        select.append(block)
        iter_orders.append(list(range(block.shape[1] + 1)))
        var_names.append(var)
    y = base._y
    if always.shape[1]:
        pinv_always = np.linalg.pinv(always)
        for i in range(len(select)):
            x = select[i]
            select[i] = x - always @ (pinv_always @ x)
        y = y - always @ (pinv_always @ y)

    def perm_to_tuple(keys, perm):
        if perm == ():
            d = {k: 0 for k, _ in keys if k is not None}
            return (0,) + tuple((k, v) for k, v in d.items())
        d = defaultdict(list)
        y_lags = []
        for v in perm:
            key = keys[v]
            if key[0] is None:
                y_lags.append(key[1])
            else:
                d[key[0]].append(key[1])
        d = dict(d)
        if not y_lags or y_lags == [0]:
            y_lags = 0
        else:
            y_lags = tuple(y_lags)
        for key in keys:
            if key[0] not in d and key[0] is not None:
                d[key[0]] = None
        for key in d:
            if d[key] is not None:
                d[key] = tuple(d[key])
        return (y_lags,) + tuple((k, v) for k, v in d.items())

    always_df = always.shape[1]
    ics = {}
    if glob:
        ar_lags = base.ar_lags if base.ar_lags is not None else []
        keys = [(None, i) for i in ar_lags]
        for k, v in base._order.items():
            keys += [(k, i) for i in v]
        x = np.column_stack([a for a in select])
        all_columns = list(range(x.shape[1]))
        for i in range(x.shape[1]):
            for perm in combinations(all_columns, i):
                key = perm_to_tuple(keys, perm)
                ics[key] = compute_ics(y, x[:, perm], always_df)
    else:
        for io in product(*iter_orders):
            x = np.column_stack([a[:, : io[i]] for i, a in enumerate(select)])
            key = [io[0] if io[0] else None]
            for j, val in enumerate(io[1:]):
                var = var_names[j]
                if causal:
                    key.append((var, None if val == 0 else val))
                else:
                    key.append((var, val - 1 if val - 1 >= 0 else None))
            key = tuple(key)
            ics[key] = compute_ics(y, x, always_df)
    index = {"aic": 0, "bic": 1, "hqic": 2}[ic]
    lowest = np.inf
    for key in ics:
        val = ics[key][index]
        if val < lowest:
            lowest = val
            selected_order = key
    exog_order = {k: v for k, v in selected_order[1:]}
    model = ARDL(
        endog,
        selected_order[0],
        exog,
        exog_order,
        trend,
        fixed=fixed,
        causal=causal,
        seasonal=seasonal,
        deterministic=deterministic,
        hold_back=hold_back,
        period=period,
        missing=missing,
    )

    return ARDLOrderSelectionResults(model, ics, trend, seasonal, period)
