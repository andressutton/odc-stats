"""
Water Observations Summaries

Water Observations Summaries are made up of:

- `count_clear`: a count of every time a pixel was observed (not obscured by terrain or clouds)
- `count_wet`: a count of every time a pixel was observed and wet
- `frequency`: what fraction of time (wet/clear) was the pixel wet

The counts are stored as `int16` and the frequency as `float32`.

There are two different Stats Plugin classes implemented in this module. The first generates summary data from
individual water observations, and the second generates a summary of summaries, which is used when generating an all
of time summary from existing annual summaries.

"""
from typing import Sequence, Tuple
import numpy as np
import xarray as xr
from datacube.model import Dataset
from datacube.utils.geometry import GeoBox
from odc.algo import safe_div, apply_numexpr, keep_good_only, binary_dilation
from odc.stac import dc_load
from ._registry import StatsPluginInterface, register


class StatsWofs(StatsPluginInterface):
    """
    Generate a Summary of Water Observations data from individual observations

    The summary is made up of counts of visible and visible and wet, and the frequency of visible and wet.

    Output data types are:
    - `count_clear`: `int16`
    - `count_wet`: `int16`
    - `frequency`: `float32`

    Special care is taken when handling NaN values and `no-data` values.
    """

    NAME = "ga_ls_wo_summary"
    SHORT_NAME = NAME
    VERSION = "1.6.0"
    PRODUCT_FAMILY = "wo_summary"

    # these get padded out if dilation was requested
    BAD_BITS_MASK = 0b0110_1000  # Cloud/Shadow + Terrain Shadow

    def __init__(
        self,
        dilation: int = 0,
        **kwargs
    ):
        super().__init__(input_bands=["water"], **kwargs)
        self._dilation = dilation  # number of pixels to pad around BAD pixels

    @property
    def measurements(self) -> Tuple[str, ...]:
        return "count_wet", "count_clear", "frequency"

    def native_transform(self, xx):
        """
        xx.water -- uint8 classifier bitmask

        .. code-block::

          |128| 64| 32| 16| |  8|  4|  2|  1|
          |---|---|---|---|=|---|---|---|---|
            7   6   5   4     3   2   1   0
            |   |   |   |     |   |   |   |
            |   |   |   |     |   |   |   x---> NODATA: 1 -- all bands were nodata
            |   |   |   |     |   |   o-------> Non Contiguous - some bands were nodata)
            |   |   |   |     |   x-----------> Low Solar Angle
            |   |   |   |     o---------------> Terrain Shadow
            |   |   |   |
            |   |   |   x---------------------> Terrain High Slope
            |   |   o-------------------------> Cloud Shadow
            |   x-----------------------------> Cloud
            o---------------------------------> Water

        out:
          .bad<Bool>   - pixel should not be counted
          .some<Bool>  - there is data (bad or good but not nodata)
          .dry<Bool>   - pixel has dry classification and is not ``bad``
          .wet<Bool>   - pixel has wet classification and is not ``bad``
        """
        if self._dilation != 0:
            xx["bad"] = binary_dilation(
                (xx.water & self.BAD_BITS_MASK) > 0, self._dilation
            ) | ((xx.water & 0b0111_1110) > 0)
        else:
            xx["bad"] = (xx.water & 0b0111_1110) > 0

        # some = (x.water&3)==0, i.e. nodata==0 and non_contigous==0
        xx["some"] = apply_numexpr("((water<<30)>>30)==0", xx, name="some")
        xx["dry"] = xx.water == 0
        xx["wet"] = xx.water == 128
        xx = xx.drop_vars("water")
        for dv in xx.data_vars.values():
            dv.attrs.pop("nodata", None)

        return xx

    @staticmethod
    def fuser(xx):
        """
        xx.bad  -- don't count
        xx.wet  -- is wet
        xx.dry  -- is dry
        xx.some -- there was at least one non-nodata observation at that pixel
        """
        from odc.algo._masking import _or_fuser

        # Merge everything with OR first
        xx = _or_fuser(xx)

        # Ensure all 3 bits are exclusive
        #  bad=T, wet=?, dry=? => (wet'=F  , dry'=F)
        #  bad=F, wet=T, dry=T => (wet'=F  , dry'=F)
        #  else                => (wet'=wet, dry'=dry)
        wet = apply_numexpr("wet & (~dry) & (~bad)", xx, dtype="bool")
        dry = apply_numexpr("dry & (~wet) & (~bad)", xx, dtype="bool")

        return xr.Dataset(dict(wet=wet, dry=dry, bad=xx.bad, some=xx.some))

    def reduce(self, xx: xr.Dataset) -> xr.Dataset:
        nodata = -999
        count_some = xx.some.sum(axis=0, dtype="int16")
        count_wet = xx.wet.sum(axis=0, dtype="int16")
        count_dry = xx.dry.sum(axis=0, dtype="int16")
        count_clear = count_wet + count_dry
        frequency = safe_div(count_wet, count_clear, dtype="float32")

        count_wet.attrs["nodata"] = nodata
        count_clear.attrs["nodata"] = nodata

        is_ok = count_some > 0
        count_wet = keep_good_only(count_wet, is_ok)
        count_clear = keep_good_only(count_clear, is_ok)

        return xr.Dataset(
            dict(
                count_wet=count_wet,
                count_clear=count_clear,
                frequency=frequency,
            )
        )


register("wofs-summary", StatsWofs)


class StatsWofsFullHistory(StatsPluginInterface):
    """
    Generate a Summary of Water Observations data from existing WO Summaries

    This is useful to turn Annual summary data into all of time summaries.

    Output data is the same as `StatsWofs` produces.
    - `count_clear`: `int16`
    - `count_wet`: `int16`
    - `frequency`: `float32`

    Special care is taken with no-data values, both to pass them through and when calculating the counts and
    frequencies.
    """

    NAME = "ga_ls_wo_fq_myear_3"
    SHORT_NAME = NAME
    VERSION = "1.6.0"
    PRODUCT_FAMILY = "wo_summary"

    def __init__(self, **kwargs):
        super().__init__(input_bands=['count_wet', 'count_clear'])

    @property
    def measurements(self) -> Tuple[str, ...]:
        return "count_wet", "count_clear", "frequency"

    def input_data(self, datasets: Sequence[Dataset], geobox: GeoBox) -> xr.Dataset:
        return dc_load(
            datasets,
            measurements=self.input_bands,
            geobox=geobox,
            chunks={},
        )

    def reduce(self, xx: xr.Dataset) -> xr.Dataset:
        dtype = xx.count_clear.dtype
        nodata = dtype.type(xx.count_clear.nodata)

        # `missing` is a record of all pixels that were never observed.
        # Store it separately first, then substitute it back in after computing the counts and
        # frequency.
        missing = (xx.count_clear == xx.count_clear.nodata).all(axis=0)
        cc = apply_numexpr(
            "where(count_clear==nodata, 0, count_clear)",
            xx,
            dtype="int16",
            casting="unsafe",
            nodata=nodata,
        ).sum(axis=0, dtype=dtype)

        cw = apply_numexpr(
            "where(count_wet==nodata, 0, count_wet)",
            xx,
            dtype=dtype,
            casting="unsafe",
            nodata=nodata,
        ).sum(axis=0, dtype=dtype)

        _yy = xr.Dataset(dict(cc=cc, cw=cw, missing=missing))

        frequency = apply_numexpr(
            "where(cc==0, _nan, (_1*cw)/(_1*cc))",
            _yy,
            dtype="float32",
            _1=np.float32(1),
            _nan=np.float32("nan"),
        )

        # Finalise the *count* variables by re-inserting the no-data value based on `missing`
        count_clear = apply_numexpr(
            "where(missing, nodata, cc)",
            _yy,
            dtype=dtype,
            nodata=nodata,
            casting="unsafe",
        )
        count_wet = apply_numexpr(
            "where(missing, nodata, cw)",
            _yy,
            dtype=dtype,
            nodata=nodata,
            casting="unsafe",
        )

        count_clear.attrs["nodata"] = int(nodata)
        count_wet.attrs["nodata"] = int(nodata)

        yy = xr.Dataset(
            dict(count_clear=count_clear, count_wet=count_wet, frequency=frequency)
        )
        return yy


register("wofs-summary-fh", StatsWofsFullHistory)
