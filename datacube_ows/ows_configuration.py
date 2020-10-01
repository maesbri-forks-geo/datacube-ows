#
#  Note this is NOT the configuration file!
#
#  This is a Python module containing the classes and functions used to load and parse configuration files.
#
#  Refer to the documentation for information on how to configure datacube_ows.
#

import os
import math
from importlib import import_module
import json

from collections.abc import Mapping

from ows import Version
from slugify import slugify

from datacube.utils import geometry
from datacube_ows.config_utils import cfg_expand, load_json_obj, import_python_obj, OWSConfigEntry, \
    OWSIndexedConfigEntry, OWSEntryNotFound, OWSExtensibleConfigEntry
from datacube_ows.cube_pool import cube, get_cube, release_cube
from datacube_ows.styles import StyleDef
from datacube_ows.ogc_utils import ConfigException, FunctionWrapper, month_date_range, local_solar_date_range, \
    year_date_range

import logging

from datacube_ows.utils import group_by_statistical

_LOG = logging.getLogger(__name__)


def read_config():
    cfg_env = os.environ.get("DATACUBE_OWS_CFG")
    cwd = None
    if not cfg_env:
        from datacube_ows.ows_cfg import ows_cfg as cfg
    elif "/" in cfg_env or cfg_env.endswith(".json"):
        cfg = load_json_obj(cfg_env)
        abs_path =  os.path.abspath(cfg_env)
        cwd = os.path.dirname(abs_path)
    elif "." in cfg_env:
        cfg = import_python_obj(cfg_env)
    elif cfg_env.startswith("{"):
        cfg = json.loads(cfg_env)
        abs_path =  os.path.abspath(cfg_env)
        cwd = os.path.dirname(abs_path)
    else:
        mod = import_module("datacube_ows.ows_cfg")
        cfg = getattr(mod, cfg_env)
    return cfg_expand(cfg, cwd=cwd)


# pylint: disable=dangerous-default-value


class BandIndex(OWSConfigEntry):
    def __init__(self, product, band_cfg, dc):
        super().__init__(band_cfg)
        self.product = product
        self.product_name = product.name
        self.native_bands = dc.list_measurements().loc[self.product_name]
        if band_cfg is None:
            self.band_cfg = {}
            for b in self.native_bands.index:
                self.band_cfg[b] = []
        else:
            self.band_cfg = band_cfg
        self._idx = {}
        self._nodata_vals = {}
        for b, aliases in self.band_cfg.items():
            if b not in self.native_bands.index:
                raise ConfigException(f"Unknown band: {b} in layer {product}")
            if b in self._idx:
                raise ConfigException(f"Duplicate band name/alias: {b} in layer {product}")
            self._idx[b] = b
            for a in aliases:
                if a != b and a in self._idx:
                    raise ConfigException(f"Duplicate band name/alias: {a} in layer {product}")
                self._idx[a] = b
            self._nodata_vals[b] = self.native_bands['nodata'][b]
            if isinstance(self._nodata_vals[b], str) and self._nodata_vals[b].lower() == "nan":
                self._nodata_vals[b] = float("nan")

    def band(self, name_alias):
        if name_alias not in self._idx:
            raise ConfigException(f"Unknown band name/alias: {name_alias} in layer {self.product}")
        return self._idx[name_alias]

    def band_label(self, name_alias):
        name = self.band(name_alias)
        if self.band_cfg[name]:
            return self.band_cfg[name][0]
        else:
            return name

    def nodata_val(self, name_alias):
        name = self.band(name_alias)
        return self._nodata_vals[name]

    def band_labels(self):
        return [self.band_label(b) for b in self.native_bands.index if b in self.band_cfg]

    def band_nodata_vals(self):
        return [self.nodata_val(b) for b in self.native_bands.index if b in self.band_cfg]


class AttributionCfg(OWSConfigEntry):
    def __init__(self, cfg):
        super().__init__(cfg)
        self.title = cfg.get("title")
        self.url = cfg.get("url")
        logo = cfg.get("logo")
        if not logo:
            self.logo_width = None
            self.logo_height = None
            self.logo_url = None
            self.logo_fmt = None
        else:
            self.logo_width = logo.get("width")
            self.logo_height = logo.get("height")
            self.logo_url = logo.get("url")
            self.logo_fmt = logo.get("format")

    @classmethod
    def parse(cls, cfg):
        if not cfg:
            return None
        else:
            return cls(cfg)


class SuppURL(OWSConfigEntry):
    @classmethod
    def parse_list(cls, cfg):
        if not cfg:
            return []
        return [ cls(u) for u in cfg ]

    def __init__(self, cfg):
        super().__init__(cfg)
        self.url = cfg["url"]
        self.format = cfg["format"]



class OWSLayer(OWSConfigEntry):
    named = False
    def __init__(self, cfg, dc, parent_layer=None, *args, **kwargs):
        super().__init__(cfg, *args, **kwargs)
        self.global_cfg = kwargs["global_cfg"]
        self.parent_layer = parent_layer

        if "title" not in cfg:
            raise ConfigException("Layer without title found under parent layer %s" % str(parent_layer))
        self.title = cfg["title"]
        try:
            if "abstract" in cfg:
                self.abstract = cfg["abstract"]
            elif parent_layer:
                self.abstract = parent_layer.abstract
            else:
                raise ConfigException("No abstract supplied for top-level layer %s" % self.title)
            # Accumulate keywords
            self.keywords = set()
            if self.parent_layer:
                for word in self.parent_layer.keywords:
                    self.keywords.add(word)
            else:
                for word in self.global_cfg.keywords:
                    self.keywords.add(word)
            for word in cfg.get("keywords", []):
                self.keywords.add(word)
            # Inherit or override attribution
            if "attribution" in cfg:
                self.attribution = AttributionCfg.parse(cfg.get("attribution"))
            elif parent_layer:
                self.attribution = self.parent_layer.attribution
            else:
                self.attribution = self.global_cfg.attribution

        except KeyError:
            raise ConfigException("Required entry missing in layer %s" % self.title)

    def layer_count(self):
        return 0

    def __str__(self):
        return "OWSLayer Config: %s" % self.title


class OWSFolder(OWSLayer):
    def __init__(self, cfg, global_cfg, dc, parent_layer=None, *args, **kwargs):
        super().__init__(cfg, dc, parent_layer, global_cfg=global_cfg, *args, **kwargs)
        self.slug_name = slugify(self.title, separator="_")
        self.child_layers = []
        if "layers" not in cfg:
            raise ConfigException("No layers section in folder layer %s" % self.title)
        for lyr_cfg in cfg["layers"]:
            try:
                lyr = parse_ows_layer(lyr_cfg, global_cfg, dc, parent_layer=self)
                self.child_layers.append(lyr)
            except ConfigException as e:
                _LOG.error("Could not load layer: %s", str(e))

    def layer_count(self):
        return sum([ l.layer_count() for l in self.child_layers ])


TIMERES_RAW = "raw"
TIMERES_MON = "month"
TIMERES_YR  = "year"

TIMERES_VALS = [ TIMERES_RAW, TIMERES_MON, TIMERES_YR]

class OWSNamedLayer(OWSExtensibleConfigEntry, OWSLayer):
    INDEX_KEYS = ["layer"]
    named = True

    def __init__(self, cfg, global_cfg, dc, parent_layer=None, *args, **kwargs):
        self.name = cfg["name"]
        super().__init__(cfg, global_cfg=global_cfg, dc=dc, parent_layer=parent_layer,
                         keyvals={"layer": self.name},
                         *args, **kwargs)
        cfg = self._raw_cfg
        self.hide = False
        try:
            self.parse_product_names(cfg)
            self.products = []
            for prod_name in self.product_names:
                if "__" in prod_name:
                    raise ConfigException("Product names cannot contain a double underscore '__'.")
                product = dc.index.products.get_by_name(prod_name)
                if not product:
                    raise ConfigException("Could not find product %s in datacube" % prod_name)
                self.products.append(product)
            self.product = self.products[0]
            self.definition = self.product.definition

            self.time_resolution = cfg.get("time_resolution", TIMERES_RAW)
            if self.time_resolution not in TIMERES_VALS:
                raise ConfigException("Invalid time resolution value %s in named layer %s" % (self.time_resolution, self.name))
        except KeyError:
            raise ConfigException("Required product names entry missing in named layer %s" % self.name)
        self.dynamic = cfg.get("dynamic", False)
        self.force_range_update(dc)
        # TODO: sub-ranges
        self.band_idx = BandIndex(self.product, cfg.get("bands"), dc)
        try:
            self.parse_resource_limits(cfg.get("resource_limits", {}))
        except KeyError:
            raise ConfigException("Missing required config items in resource limits for layer %s" % self.name)
        try:
            self.parse_flags(cfg.get("flags", {}), dc)
        except KeyError:
            raise ConfigException("Missing required config items in flags section for layer %s" % self.name)
        try:
            self.parse_image_processing(cfg["image_processing"])
        except KeyError:
            raise ConfigException("Missing required config items in image processing section for layer %s" % self.name)
        self.identifiers = cfg.get("identifiers", {})
        for auth in self.identifiers.keys():
            if auth not in self.global_cfg.authorities:
                raise ConfigException("Identifier with non-declared authority: %s" % repr(auth))
        try:
            self.parse_urls(cfg.get("urls", {}))
        except KeyError:
            raise ConfigException("Missing required config items in urls section for layer %s" % self.name)
        self.parse_feature_info(cfg.get("feature_info", {}))

        self.feature_info_include_utc_dates = cfg.get("feature_info_url_dates", False)
        try:
            self.parse_styling(cfg["styling"])
        except KeyError:
            raise ConfigException("Missing required config items in styling section for layer %s" % self.name)

        if self.global_cfg.wcs:
            try:
                self.parse_wcs(cfg.get("wcs"), dc)
            except KeyError:
                raise ConfigException("Missing required config items in wcs section for layer %s" % self.name)

        sub_prod_cfg = cfg.get("sub_products", {})
        self.sub_product_label = sub_prod_cfg.get("label")
        if "extractor" in sub_prod_cfg:
            self.sub_product_extractor = FunctionWrapper(self, sub_prod_cfg["extractor"])
        else:
            self.sub_product_extractor = None

        # And finally, add to the global product index.
        self.global_cfg.product_index[self.name] = self
        if not self.multi_product:
            self.global_cfg.native_product_index[self.product_name] = self

    def parse_resource_limits(self, cfg):
        wms_cfg = cfg.get("wms", {})
        wcs_cfg = cfg.get("wcs", {})
        self.zoom_fill = wms_cfg.get("zoomed_out_fill_colour", [150, 180, 200, 160])
        self.min_zoom = wms_cfg.get("min_zoom_factor", 300.0)
        self.max_datasets_wms = wms_cfg.get("max_datasets", 0)
        self.max_datasets_wcs = wcs_cfg.get("max_datasets", 0)

    def parse_image_processing(self, cfg):
        emf_cfg = cfg["extent_mask_func"]
        if isinstance(emf_cfg, Mapping) or isinstance(emf_cfg, str):
            self.extent_mask_func = [ FunctionWrapper(self, emf_cfg) ]
        else:
            self.extent_mask_func = list([ FunctionWrapper(self, emf) for emf in emf_cfg ])
        raw_afb = cfg.get("always_fetch_bands", [])
        self.always_fetch_bands = list([ self.band_idx.band(b) for b in raw_afb ])
        self.solar_correction = cfg.get("apply_solar_corrections", False)
        self.data_manual_merge = cfg.get("manual_merge", False)
        if self.solar_correction and not self.data_manual_merge:
            raise ConfigException("Solar correction requires manual_merge.")
        if self.data_manual_merge and not self.solar_correction:
            _LOG.warning("Manual merge is only recommended where solar correction is required.")

        if cfg.get("fuse_func"):
            self.fuse_func = FunctionWrapper(self, cfg["fuse_func"])
        else:
            self.fuse_func = None

    def parse_feature_info(self, cfg):
        self.feature_info_include_utc_dates = cfg.get("include_utc_dates", False)
        custom = cfg.get("include_custom", {})
        self.feature_info_custom_includes = { k: FunctionWrapper(self, v) for k,v in custom.items() }

    def parse_flags(self, cfg, dc):
        if cfg:
            self.parse_pq_names(cfg)
            self.pq_band = cfg["band"]
            if "fuse_func" in cfg:
                self.pq_fuse_func = FunctionWrapper(self, cfg["fuse_func"])
            else:
                self.pq_fuse_func = None
            self.pq_ignore_time = cfg.get("ignore_time", False)
            self.ignore_info_flags = cfg.get("ignore_info_flags", [])
            self.pq_manual_merge = cfg.get("manual_merge", False)
        else:
            self.pq_names = []
            self.pq_name = None
            self.pq_band = None
            self.pq_ignore_time = False
            self.ignore_info_flags = []
            self.pq_manual_merge = False
        self.pq_products = []

        if self.pq_names:
            for pqn in self.pq_names:
                if pqn is not None:
                    pq_product = dc.index.products.get_by_name(pqn)
                    if pq_product is None:
                        raise ConfigException("Could not find pq_product %s for %s in database" % (pqn, self.name))
                    self.pq_products.append(pq_product)

        self.info_mask = ~0
        if self.pq_products:
            self.pq_product = self.pq_products[0]
            meas = self.pq_product.lookup_measurements([self.pq_band])
            self.flags_def = meas[self.pq_band]["flags_definition"]
            for bitname in self.ignore_info_flags:
                bit = self.flags_def[bitname]["bits"]
                if not isinstance(bit, int):
                    continue
                flag = 1 << bit
                self.info_mask &= ~flag
        else:
            self.pq_product = None

    def parse_urls(self, cfg):
        self.feature_list_urls = SuppURL.parse_list(cfg.get("features", []))
        self.data_urls = SuppURL.parse_list(cfg.get("data", []))

    def parse_styling(self, cfg):
        self.styles = []
        self.style_index = {}
        for scfg in cfg["styles"]:
            style = StyleDef(self, scfg)
            self.styles.append(style)
            self.style_index[style.name] = style
        if "default_style" in cfg:
            if cfg["default_style"] not in self.style_index:
                raise ConfigException("Default style %s is not in the 'styles' for layer %s" % (
                    cfg["default_style"], self.name
                ))
            self.default_style = self.style_index[cfg["default_style"]]
        else:
            self.default_style = self.styles[0]

    def parse_wcs(self, cfg, dc):
        if cfg is None:
            self.wcs = False
            return
        else:
            self.wcs = True
        # Native CRS
        try:
            self.native_CRS = self.product.definition["storage"]["crs"]
            if cfg.get("native_crs") == self.native_CRS:
                _LOG.debug(
                    "Native crs for layer %s is specified in ODC metadata and does not need to be specified in configuration",
                    self.name)
            else:
                _LOG.warning("Native crs for layer %s is specified in config as %s - overridden to %s by ODC metadata",
                             self.name, cfg['native_crs'], self.native_CRS)
        except KeyError:
            self.native_CRS = cfg.get("native_crs")

        if not self.native_CRS:
            raise ConfigException(f"No native CRS could be found for layer {self.name}")
        if self.native_CRS not in self.global_cfg.published_CRSs:
            raise ConfigException("Native CRS for product %s (%s) not in published CRSs" % (
                            self.product_name,
                            self.native_CRS))
        self.native_CRS_def = self.global_cfg.published_CRSs[self.native_CRS]
        # Prepare Rectified Grids
        try:
            native_bounding_box = self.bboxes[self.native_CRS]
        except KeyError:
            _LOG.warning("Layer: %s No bounding box in ranges for native CRS %s - rerun update_ranges.py",
                         self.name,
                         self.native_CRS)
            self.hide = True
            return
        self.origin_x = native_bounding_box["left"]
        self.origin_y = native_bounding_box["bottom"]

        try:
            self.resolution_x = self.product.definition["storage"]["resolution"][self.native_CRS_def["horizontal_coord"]]
            self.resolution_y = self.product.definition["storage"]["resolution"][self.native_CRS_def["vertical_coord"]]
        except KeyError:
            self.resolution_x = None
            self.resolution_y = None

        if self.resolution_x is None:
            try:
                self.resolution_x, self.resolution_y = cfg["native_resolution"]
            except KeyError:
                raise ConfigException(f"No native resolution supplied for WCS enabled layer {self.name}")
            except ValueError:
                raise ConfigException(f"Invalid native resolution supplied for WCS enabled layer {self.name}")
            except TypeError:
                raise ConfigException(f"Invalid native resolution supplied for WCS enabled layer {self.name}")
        elif "native_resolution" in cfg:
            config_x, config_y = (float(r) for r in cfg["native_resolution"])
            if (
                    math.isclose(config_x, float(self.resolution_x), rel_tol=1e-8)
                and math.isclose(config_y, float(self.resolution_y), rel_tol=1e-8)
                ):
                _LOG.debug("Native resolution for layer %s is specified in ODC metadata and does not need to be specified in configuration",
                           self.name)
            else:
                _LOG.warning("Native resolution for layer %s is specified in config as %s - overridden to (%.15f, %.15f) by ODC metadata",
                             self.name, repr(cfg['native_resolution']), self.resolution_x, self.resolution_y)

        if (native_bounding_box["right"] - native_bounding_box["left"]) < self.resolution_x:
            ConfigException("Native (%s) bounding box on layer %s has left %.8f, right %.8f (diff %d), but horizontal resolution is %.8f"
                            % (
                                self.native_CRS,
                                self.name,
                                native_bounding_box["left"],
                                native_bounding_box["right"],
                                native_bounding_box["right"] - native_bounding_box["left"],
                                self.resolution_x

                            ))
        if (native_bounding_box["top"] - native_bounding_box["bottom"]) < self.resolution_x:
            ConfigException("Native (%s) bounding box on layer %s has bottom %f, top %f (diff %d), but vertical resolution is %f"
                            % (
                                self.native_CRS,
                                self.name,
                                native_bounding_box["bottom"],
                                native_bounding_box["top"],
                                native_bounding_box["top"] - native_bounding_box["bottom"],
                                self.resolution_y

            ))
        self.grid_high_x = int((native_bounding_box["right"] - native_bounding_box["left"]) / self.resolution_x)
        self.grid_high_y = int((
                                       native_bounding_box["top"] - native_bounding_box["bottom"]) / self.resolution_y)

        if self.grid_high_x == 0:
            err_str = f"Grid High X is zero on layer {self.name}: native ({self.native_CRS}) extent: {native_bounding_box['left']},{native_bounding_box['right']}: x_res={self.resolution_x}"
            raise ConfigException(err_str)
        if self.grid_high_y == 0:
            err_str = f"Grid High y is zero on layer {self.name}: native ({self.native_CRS}) extent: {native_bounding_box['bottom']},{native_bounding_box['top']}: x_res={self.resolution_y}"
            raise ConfigException(err_str)
        self.grids = {}
        for crs, crs_def in self.global_cfg.published_CRSs.items():
            if crs == self.native_CRS:
                self.grids[crs] = {
                    "origin": (self.origin_x, self.origin_y),
                    "resolution": (self.resolution_x, self.resolution_y),
                }
            else:
                try:
                    bbox = self.bboxes[crs]
                except KeyError:
                    continue
                self.grids[crs] = {
                    "origin": (bbox["left"], bbox["bottom"]),
                    "resolution": (
                        (bbox["right"] - bbox["left"]) / self.grid_high_x,
                        (bbox["top"] - bbox["bottom"]) / self.grid_high_y
                    )
                }

        # Band management
        self.wcs_default_bands = [self.band_idx.band(b) for b in cfg["default_bands"]]
        # Cache some metadata from the datacube
        try:
            bands = dc.list_measurements().loc[self.product_name]
        except KeyError:
            raise ConfigException("Datacube.list_measurements() not returning measurements for product %s" %
                                  self.product_name)
        self.bands = bands.index.values
        try:
            self.nodata_values = bands['nodata'].values
        except KeyError:
            raise ConfigException("Datacube has no 'nodata' values for bands in product %s" % self.product_name)
        self.nodata_dict = {a: b for a, b in zip(self.bands, self.nodata_values)}

        # Native format
        if "native_format" in cfg:
            self.native_format = cfg["native_format"]
            if self.native_format not in self.global_cfg.wcs_formats_by_name:
                raise ConfigException("WCS native format for layer %s is not in supported formats list" % self.product_name)
        else:
            self.native_format = self.global_cfg.native_wcs_format

    def parse_product_names(self, cfg):
        raise NotImplementedError()

    def parse_pq_names(self, cfg):
        raise NotImplementedError()

    def force_range_update(self, ext_dc=None):
        if ext_dc:
            dc = ext_dc
        else:
            dc = get_cube()
        self.hide = False
        self._ranges = None
        try:
            from datacube_ows.product_ranges import get_ranges
            self._ranges = get_ranges(dc, self)
            if self._ranges is None:
                raise Exception("Null product range")
            self.bboxes = self.extract_bboxes()
        # pylint: disable=broad-except
        except Exception as a:
            _LOG.warning("get_ranges failed for layer %s: %s", self.name, str(a))
            self.hide = True
            self.bboxes = {}
        finally:
            if not ext_dc:
                release_cube(dc)

    @property
    def ranges(self):
        if self.dynamic:
            self.force_range_update()
        return self._ranges

    def extract_bboxes(self):
        if self._ranges is None:
            return {}
        bboxes = {}
        for crs_id, bbox in self._ranges["bboxes"].items():
            if crs_id in self.global_cfg.published_CRSs:
                if self.global_cfg.published_CRSs[crs_id].get("vertical_coord_first"):
                    bboxes[crs_id] = {
                        "right": bbox["bottom"],
                         "left": bbox["top"],
                         "top": bbox["left"],
                         "bottom": bbox["right"]
                     }
                else:
                    bboxes[crs_id] = {
                        "right": bbox["right"],
                        "left": bbox["left"],
                        "top": bbox["top"],
                        "bottom": bbox["bottom"]
                    }
        return bboxes

    def layer_count(self):
        return 1

    @property
    def is_raw_time_res(self):
        return self.time_resolution == TIMERES_RAW

    @property
    def is_month_time_res(self):
        return self.time_resolution == TIMERES_MON

    @property
    def is_year_time_res(self):
        return self.time_resolution == TIMERES_YR

    def search_times(self, t, geobox):
        if self.is_month_time_res:
            return month_date_range(t)
        elif self.is_year_time_res:
            return year_date_range(t)
        else:
            return local_solar_date_range(geobox, t)

    def dataset_groupby(self):
        if self.is_raw_time_res:
            return "solar_day"
        else:
            return group_by_statistical()

    def __str__(self):
        return "Named OWSLayer: %s" % self.name

    def lookup(self, cfg, keyvals, subs=None):
        if not subs and "layer" not in keyvals:
            subs = {
                "layer": self.product
            }
    @classmethod
    def lookup_impl(cls, cfg, keyvals, keyval_subs=None):
        try:
            return cfg.global_cfg.product_index[keyvals["layer"]]
        except KeyError:
            raise OWSEntryNotFound(f"Layer {keyvals['layer']} not found")


class OWSProductLayer(OWSNamedLayer):
    multi_product = False

    def parse_product_names(self, cfg):
        self.product_name = cfg["product_name"]
        self.product_names = [ self.product_name ]

    def parse_pq_names(self, cfg):
        # pylint: disable=attribute-defined-outside-init
        if "dataset" in cfg:
            self.pq_name = cfg["dataset"]
            print("CFG WARNING:",
                  "The preferred name for the 'dataset' entry",
                  "in the flags section is now 'product'.",
                  "Please update the configuration for layer",
                  self.name)
        elif "product" in cfg:
            self.pq_name = cfg["product"]
        else:
            self.pq_name = self.product_name
        self.pq_names = [ self.pq_name ]


class OWSMultiProductLayer(OWSNamedLayer):
    multi_product = True

    def parse_product_names(self, cfg):
        self.product_names = cfg["product_names"]
        self.product_name = self.product_names[0]

    def parse_pq_names(self, cfg):
        # pylint: disable=attribute-defined-outside-init
        if "datasets" in cfg:
            self.pq_names = cfg["datasets"]
            print("CFG WARNING:",
                  "The preferred name for the 'datasets' entry",
                  "in the flags section is now 'products'.",
                  "Please update the configuration for layer",
                  self.name)
        elif "products" in cfg:
            self.pq_names = cfg["products"]
        else:
            self.pq_names = list(self.product_names)
        self.pq_name = self.pq_names[0]


def parse_ows_layer(cfg, global_cfg, dc, parent_layer=None):
    if cfg.get("name", None):
        if cfg.get("multi_product", False):
            return OWSMultiProductLayer(cfg, global_cfg, dc, parent_layer)
        else:
            return OWSProductLayer(cfg, global_cfg, dc, parent_layer)
    else:
        return OWSFolder(cfg, global_cfg, dc, parent_layer)


class WCSFormat:
    @staticmethod
    def from_cfg(cfg):
        renderers = []
        for name, fmt in cfg.items():
            if "renderers" in fmt:
                renderers.append(
                    WCSFormat(
                        name,
                        fmt["mime"],
                        fmt["extension"],
                        fmt["renderers"],
                        fmt.get("multi-time", False)
                    )
                )
            elif "renderer" in fmt:
                _LOG.warning("'renderer' in WCS format declarations is "
                      "deprecated. Please review the latest example config "
                      "file and update your config file accordingly. Format %s "
                      "will be WCS 1 only.", name)
                renderers.append(
                    WCSFormat(
                        name,
                        fmt["mime"],
                        fmt["extension"],
                        {"1": fmt["renderer"]},
                        fmt.get("multi-time", False)
                    )
                )
        return renderers

    def __init__(self, name, mime, extension, renderers,
                 multi_time):
        self.name = name
        self.mime = mime
        self.extension = extension
        self.multi_time = multi_time
        self.renderers = {
            int(ver): FunctionWrapper(None, renderer)
            for ver, renderer in renderers.items()
        }
        if 1 not in self.renderers:
            _LOG.warning("No renderer supplied for WCS 1.x for format %s", self.name)
        if 2 not in self.renderers:
            _LOG.warning("Warning: No renderer supplied for WCS 2.x for format %s", self.name)

    def renderer(self, version):
        if isinstance(version, str):
            version = int(version.split(".")[0])
        elif isinstance(version, Version):
            version = version.major
        return self.renderers[version]


class OWSConfig(OWSConfigEntry):
    _instance = None
    initialised = False

    def __new__(cls, *args, **kwargs):
        if not cls._instance:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, refresh=False):
        if not self.initialised or refresh:
            self.initialised = True
            cfg = read_config()
            super().__init__(cfg)
            try:
                self.parse_global(cfg["global"])
            except KeyError as e:
                raise ConfigException(
                    "Missing required config entry in 'global' section: %s" % str(e)
                )

            if self.wms:
                self.parse_wms(cfg.get("wms", {}))
            else:
                self.parse_wms({})

            if self.wcs:
                try:
                    self.parse_wcs(cfg["wcs"])
                except KeyError as e:
                    raise ConfigException(
                        "Missing required config entry in 'wcs' section (with WCS enabled): %s" % str(e)
                    )
            else:
                self.parse_wcs(None)
            try:
                self.parse_layers(cfg["layers"])
            except KeyError as e:
                raise ConfigException("Missing required config entry in 'layers' section")

    def parse_global(self, cfg):
        self._response_headers = cfg.get("response_headers", {})
        self.wms = cfg.get("services", {}).get("wms", True)
        self.wmts = cfg.get("services", {}).get("wmts", True)
        self.wcs = cfg.get("services", {}).get("wcs", False)
        if not self.wms and not self.wmts and not self.wcs:
            raise ConfigException("At least one service must be active.")
        self.title = cfg["title"]
        self.allowed_urls = cfg["allowed_urls"]
        self.info_url = cfg["info_url"]
        self.abstract = cfg.get("abstract")
        self.contact_info = cfg.get("contact_info", {})
        self.keywords = cfg.get("keywords", [])
        self.fees = cfg.get("fees")
        self.access_constraints = cfg.get("access_constraints")
        # self.use_extent_views = cfg.get("use_extent_views", False)
        if not self.fees:
            self.fees = "none"
        if not self.access_constraints:
            self.access_constraints = "none"
        def make_gml_name(name):
            if name.startswith("EPSG:"):
                return f"http://www.opengis.net/def/crs/EPSG/0/{name[5:]}"
            else:
                return name

        self.published_CRSs = {}
        self.internal_CRSs = {}
        CRS_aliases = {}
        for crs_str, crsdef in cfg["published_CRSs"].items():
            if "alias" in crsdef:
                CRS_aliases[crs_str] = crsdef
                continue
            self.internal_CRSs[crs_str] = {
                "geographic": crsdef["geographic"],
                "horizontal_coord": crsdef.get("horizontal_coord", "longitude"),
                "vertical_coord": crsdef.get("vertical_coord", "latitude"),
                "vertical_coord_first": crsdef.get("vertical_coord_first", False),
                "gml_name": make_gml_name(crs_str),
                "alias_of": None
            }
            self.published_CRSs[crs_str] = self.internal_CRSs[crs_str]
            if self.published_CRSs[crs_str]["geographic"]:
                if self.published_CRSs[crs_str]["horizontal_coord"] != "longitude":
                    raise Exception("Published CRS {} is geographic"
                                    "but has a horizontal coordinate that is not 'longitude'".format(crs_str))
                if self.published_CRSs[crs_str]["vertical_coord"] != "latitude":
                    raise Exception("Published CRS {} is geographic"
                                    "but has a vertical coordinate that is not 'latitude'".format(crs_str))
        for alias, alias_def in CRS_aliases.items():
            target_crs = alias_def["alias"]
            if target_crs not in self.published_CRSs:
                _LOG.warning("CRS %s defined as alias for %s, which is not a published CRS - skipping",
                             alias, target_crs)
                continue
            target_def = self.published_CRSs[target_crs]
            self.published_CRSs[alias] = target_def.copy()
            self.published_CRSs[alias]["gml_name"] = make_gml_name(alias)
            self.published_CRSs[alias]["alias_of"] = target_crs

    def parse_wms(self, cfg):
        if not self.wms and not self.wmts:
            cfg = {}
        self.s3_bucket = cfg.get("s3_bucket", "")
        self.s3_url = cfg.get("s3_url", "")
        self.s3_aws_zone = cfg.get("s3_aws_zone", "")
        self.wms_max_width = cfg.get("max_width", 256)
        self.wms_max_height = cfg.get("max_height", 256)
        self.attribution = AttributionCfg.parse(cfg.get("attribution"))
        self.authorities = cfg.get("authorities", {})

    def parse_wcs(self, cfg):
        if self.wcs:
            if not isinstance(cfg, Mapping):
                raise ConfigException("WCS section missing (and WCS is enabled)")
            self.wcs_formats = WCSFormat.from_cfg(cfg["formats"])
            self.wcs_formats_by_name = {
                fmt.name: fmt
                for fmt in self.wcs_formats
            }
            self.wcs_formats_by_mime = {
                fmt.mime: fmt
                for fmt in self.wcs_formats
            }
            if not self.wcs_formats:
                raise ConfigException("Must configure at least one wcs format to support WCS.")

            self.native_wcs_format = cfg["native_format"]
            if self.native_wcs_format not in self.wcs_formats_by_name:
                raise Exception("Configured native WCS format not a supported format.")
        else:
            self.default_geographic_CRS = None
            self.default_geographic_CRS_def = None
            self.wcs_formats = []
            self.wcs_formats_by_name = {}
            self.wcs_formats_by_mime = {}
            self.native_wcs_format = None
        # shouldn't need to keep these?
        # self.dummy_wcs_grid = False
        # self.create_wcs_grid = False

    def parse_layers(self, cfg):
        self.layers = []
        self.product_index = {}
        self.native_product_index = {}
        with cube() as dc:
            if dc:
                for lyr_cfg in cfg:
                    self.layers.append(parse_ows_layer(lyr_cfg, self, dc))

    def alias_bboxes(self, bboxes):
        out = {}
        for crsid, crsdef in self.published_CRSs.items():
            a_crsid = crsdef["alias_of"]
            if a_crsid:
                if a_crsid in bboxes:
                    out[crsid] = bboxes[a_crsid]
            else:
                if crsid in bboxes:
                    out[crsid] = bboxes[crsid]
        return out

    def crs(self, crsid):
        if crsid not in self.published_CRSs:
            raise ConfigException(f"CRS {crsid} is not published")
        crs_def = self.published_CRSs[crsid]
        crs_alias = crs_def["alias_of"]
        if crs_alias:
            use_crs = crs_alias
        else:
            use_crs = crsid
        return geometry.CRS(use_crs)

    def response_headers(self, d):
        hdrs = self._response_headers.copy()
        hdrs.update(d)
        return hdrs


def get_config(refresh=False):
    return OWSConfig(refresh=refresh)