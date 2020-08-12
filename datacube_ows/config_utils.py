import json
import os
from importlib import import_module
from typing import Mapping, Sequence

from datacube_ows.ogc_utils import ConfigException


def cfg_expand(cfg_unexpanded, cwd=None, inclusions=[]):
    # inclusions defaulting to an empty list is dangerous, but note that it is never modified.
    # If modification of inclusions is a required, a copy (ninclusions) is made and modified instead.
    if cwd is None:
        cwd = os.getcwd()
    if isinstance(cfg_unexpanded, Mapping):
        if "include" in cfg_unexpanded:
            if cfg_unexpanded["include"] in inclusions:
                raise ConfigException("Cyclic inclusion: %s" % cfg_unexpanded["include"])
            ninclusions = inclusions.copy()
            ninclusions.append(cfg_unexpanded["include"])
            # Perform expansion
            if "type" not in cfg_unexpanded or cfg_unexpanded["type"] == "json":
                # JSON Expansion
                raw_path = cfg_unexpanded["include"]
                try:
                    # Try in actual working directory
                    json_obj = load_json_obj(raw_path)
                    abs_path = os.path.abspath(cfg_unexpanded["include"])
                    cwd = os.path.dirname(abs_path)
                # pylint: disable=broad-except
                except Exception:
                    json_obj = None
                if json_obj is None:
                    path = os.path.join(cwd, raw_path)
                    try:
                        # Try in inherited working directory
                        json_obj = load_json_obj(path)
                        abs_path = os.path.abspath(path)
                        cwd = os.path.dirname(abs_path)
                    # pylint: disable=broad-except
                    except Exception:
                        json_obj = None
                if json_obj is None:
                    raise ConfigException("Could not find json file %s" % raw_path)
                return cfg_expand(load_json_obj(abs_path), cwd=cwd, inclusions=ninclusions)
            elif cfg_unexpanded["type"] == "python":
                # Python Expansion
                return cfg_expand(import_python_obj(cfg_unexpanded["include"]), cwd=cwd, inclusions=ninclusions)
            else:
                raise ConfigException("Unsupported inclusion type: %s" % str(cfg_unexpanded["type"]))
        else:
            return { k: cfg_expand(v, cwd=cwd, inclusions=inclusions) for k,v in cfg_unexpanded.items()  }
    elif isinstance(cfg_unexpanded, Sequence) and not isinstance(cfg_unexpanded, str):
        return list([cfg_expand(elem, cwd=cwd, inclusions=inclusions) for elem in cfg_unexpanded ])
    else:
        return cfg_unexpanded


def load_json_obj(path):
    with open(path) as json_file:
        return json.load(json_file)


def import_python_obj(path):
    """Imports a python dictionary by fully-qualified path

    :return: a Callable object, or None
    """
    mod_name, obj_name = path.rsplit('.', 1)
    mod = import_module(mod_name)
    obj = getattr(mod, obj_name)
    return obj


def accum_min(a, b):
    if a is None:
        return b
    elif b is None:
        return a
    else:
        return min(a, b)


def accum_max(a, b):
    if a is None:
        return b
    elif b is None:
        return a
    else:
        return max(a, b)


class OWSConfigEntry(object):
    def __init__(self, cfg):
        self._raw_cfg = cfg