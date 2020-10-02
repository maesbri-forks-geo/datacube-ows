import datacube_ows.config_utils
import datacube_ows.ogc_utils

import datacube_ows.ows_configuration
from datacube_ows.ows_configuration import BandIndex
from unittest.mock import patch, MagicMock
import pytest

def test_accum_max():
    ret = datacube_ows.config_utils.accum_max(1, 3)
    assert ret == 3

def test_accum_min():
    ret = datacube_ows.config_utils.accum_min(1, 3)
    assert ret == 1

def test_band_index():
    dc = MagicMock()
    prod = MagicMock()
    prod.name = "prod_name"
    nb = MagicMock()
    nb.index = ['band1', 'band2', 'band3', 'band4']
    nb.get.return_val = {
        "band1": -999,
        "band2": -999,
        "band3": -999,
        "band4": -999,
    }
    dc.list_measurements().loc = {
        "prod_name": nb
    }

    foo =dc.list_measurements().loc["prod_name"]

    cfg = {
        "band1": [],
        "band2": ["alias1"],
        "band3": ["alias2", "alias3"],
        "band4": ["band4", "alias4"],
    }

    bidx = BandIndex(prod, cfg, dc)



def test_function_wrapper_lyr():
    lyr = MagicMock()
    func_cfg = "tests.utils.test_function"
    f = datacube_ows.ogc_utils.FunctionWrapper(lyr, func_cfg)
    assert f(7)[0] == "a7  b2  c3"
    assert f.band_mapper is None
    func_cfg = {
        "function": "tests.utils.test_function",
    }
    f = datacube_ows.ogc_utils.FunctionWrapper(lyr, func_cfg)
    assert f(7, 8)[0] == "a7  b8  c3"
    func_cfg = {
        "function": "tests.utils.test_function",
        "kwargs": {
            "foo": "bar",
            "c": "ouple"
        }
    }
    f = datacube_ows.ogc_utils.FunctionWrapper(lyr, func_cfg)
    result = f("pple", "eagle")
    assert result[0] == "apple  beagle  couple"
    assert result[1]["foo"] == "bar"
    assert f.band_mapper is None

