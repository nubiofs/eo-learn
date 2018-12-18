"""
Module for transforming data between different types
"""

import pandas
import numpy as np
import rasterio.features
import rasterio.transform
import shapely.geometry

from geopandas import GeoSeries, GeoDataFrame

from eolearn.core import EOTask, FeatureType


class VectorToRaster(EOTask):
    """
    Task burns into one of the EOPatch's features geo-referenced shapes given in provided Geopandas DataFrame.

    :param feature: A tuple of feature type and feature name, e.g. (FeatureType.MASK, 'cloud_mask')
    :type feature: (FeatureType, str)
    :param vector_data: Vector data
    :type vector_data: geopandas.GeoDataFrame
    :param raster_value: Value of raster pixels which are contained inside of vector polygons
    :type raster_value: int or float
    :param raster_shape: Can be a tuple in form of (height, width) of an existing feature from which the shape will be
                            taken e.g. (FeatureType.MASK, 'IS_DATA')
    :type raster_shape: (int, int) or (FeatureType, str)
    :param raster_dtype: `numpy` data type of the obtained raster array
    :type raster_dtype: numpy.dtype
    :param no_data_value: Value of raster pixels which are outside of vector polygons
    :type no_data_value: int or float
    :param params: Additional parameters which will be passed to `rasterio.features.rasterize`. Current options are
        parameters `all_touched` and `merge_alg`
    """
    def __init__(self, feature, vector_data, raster_value, raster_shape, raster_dtype=np.uint8, no_data_value=0,
                 **params):
        self.feature_type, self.feature_name = next(iter(self._parse_features(feature)))
        self.vector_data = vector_data
        self.raster_value = raster_value
        self.raster_shape = raster_shape
        self.raster_dtype = raster_dtype
        self.no_data_value = no_data_value
        self.params = params

    def _get_submap(self, eopatch):
        """
        Returns a new geopandas dataframe with same structure as original one (columns) except that
        it contains only polygons that are contained within the given bbox.

        :param eopatch: input EOPatch
        :type eopatch: EOPatch
        :return: New EOPatch
        :rtype: EOPatch
        """
        bbox_poly = eopatch.bbox.get_geometry()

        filtered_data = self.vector_data[self.vector_data.geometry.intersects(bbox_poly)].copy(deep=True)
        filtered_data.geometry = filtered_data.geometry.intersection(bbox_poly)

        return filtered_data

    def _get_shape(self, eopatch):
        if isinstance(self.raster_shape, (tuple, list)) and len(self.raster_shape) == 2:
            if isinstance(self.raster_shape[0], int) and isinstance(self.raster_shape[1], int):
                return self.raster_shape

            feature_type, feature_name = next(self._parse_features(self.raster_shape)(eopatch))
            return eopatch.get_spatial_dimension(feature_type, feature_name)

        raise ValueError('Could not determine shape of the raster image')

    def execute(self, eopatch):
        """ Execute function which adds new vector layer to the EOPatch

        :param eopatch: input EOPatch
        :type eopatch: EOPatch
        :return: New EOPatch with added vector layer
        :rtype: EOPatch
        """
        bbox_map = self._get_submap(eopatch)
        height, width = self._get_shape(eopatch)
        data_transform = rasterio.transform.from_bounds(*eopatch.bbox, width=width, height=height)

        if self.feature_name in eopatch[self.feature_type]:
            raster = eopatch[self.feature_type][self.feature_name].squeeze()
        else:
            raster = np.ones((height, width), dtype=self.raster_dtype) * self.no_data_value

        if not bbox_map.empty:
            rasterio.features.rasterize([(bbox_map.cascaded_union.buffer(0), self.raster_value)], out=raster,
                                        transform=data_transform, dtype=self.raster_dtype, **self.params)

        eopatch[self.feature_type][self.feature_name] = raster[..., np.newaxis]

        return eopatch


class RasterToVector(EOTask):
    """
    Task that turns raster mask feature into vector feature. Each connected component with the same value on the raster
    mask is turned into a shapely polygon. Polygon are returned as a geometry column in a ``geopandas.GeoDataFrame``
    structure together with a column `VALUE` with values of each polygon.
    If raster mask feature has time component, vector feature will also have a column `TIMESTAMP` with timestamps to
    which raster image each polygon belongs to.
    If raster mask has multiple channels each of them will be vectorized separately but polygons will be in the
    same vector feature
    """
    def __init__(self, features, values=None, value_column_name='VALUE', raster_dtype=None, **params):
        """
        :param features: One or more raster mask features which will be vectorized together with an optional new name
        of vector feature. If no new name is given the same name will be used.

        Examples:
            features=(FeatureType.MASK, 'CLOUD_MASK', 'VECTOR_CLOUD_MASK')

            features=[(FeatureType.MASK_TIMELESS, 'CLASSIFICATION'), (FeatureType.MASK, 'MONOTEMPORAL_CLASSIFICATION')]

        :type features: object supported by eolearn.core.utilities.FeatureParser class
        :param values: List of values which will be vectorized. By default is set to ``None`` and all values will be
            vectorized
        :type values: list(int) or None
        :param value_column_name: Name of a column in geopandas data frame where values of rasterized polygons will
            be written. If set to `None` no such column will be created.
        :type value_column_name: str or None
        :param raster_dtype: If raster feature mask is of type which is not supported by ``rasterio.features.shapes``
            (e.g. ``numpy.int64``) this parameter is used to cast the mask into a different type
            (``numpy.int16``, ``numpy.int32``, ``numpy.uint8``, ``numpy.uint16`` or ``numpy.float32``). By default
            value of the parameter is ``None`` and no casting is done.
        :type raster_dtype: numpy.dtype or None
        :param params: Additional parameters which will be passed to `rasterio.features.shapes`. Current option is
            parameter `connectivity`
        """
        self.feature_gen = self._parse_features(features, new_names=True)
        self.values = values
        self.value_column_name = value_column_name
        self.raster_dtype = raster_dtype
        self.params = params

        for feature_type, _, _ in self.feature_gen:
            if not (feature_type.is_spatial() and feature_type.is_discrete()):
                raise ValueError('Input features should be a spatial mask, but {} found'.format(feature_type))

    def _vectorize_single_raster(self, raster, data_transform, crs, timestamp=None):
        """ Vectorizes a data slice of a single time component

        :param raster: Numpy array or shape (height, width, channels)
        :type raster: numpy.ndarray
        :param data_transform: Object holding a transform vector (i.e. geographical location vector) of the raster
        :type data_transform: affine.Affine
        :param crs: Coordinate reference system
        :type crs: sentinelhub.CRS
        :param timestamp: Time of the data slice
        :type timestamp: datetime.datetime
        :return: Vectorized data
        :rtype: geopandas.GeoDataFrame
        """
        mask = None
        if self.values:
            mask = np.zeros(raster.shape, dtype=np.bool)
            for value in self.values:
                mask[raster == value] = True

        geo_list = []
        value_list = []
        for idx in range(raster.shape[-1]):
            for geojson, value in rasterio.features.shapes(raster[..., idx],
                                                           mask=None if mask is None else mask[..., idx],
                                                           transform=data_transform, **self.params):
                geo_list.append(shapely.geometry.shape(geojson))
                value_list.append(value)

        series_dict = dict(geometry=GeoSeries(geo_list))

        if self.value_column_name is not None:
            series_dict[self.value_column_name] = GeoSeries(value_list)

        if timestamp is not None:
            series_dict[FeatureType.TIMESTAMP.value] = GeoSeries([timestamp] * len(geo_list))

        return GeoDataFrame(series_dict, crs={'init': 'epsg:{}'.format(crs.value)})

    def execute(self, eopatch):
        """ Execute function which adds new vector layer to the EOPatch

        :param eopatch: input EOPatch
        :type eopatch: EOPatch
        :return: New EOPatch with added vector layer
        :rtype: EOPatch
        """
        for raster_ft, raster_fn, vector_fn in self.feature_gen(eopatch):
            vector_ft = FeatureType.VECTOR_TIMELESS if raster_ft.is_timeless() else FeatureType.VECTOR

            raster = eopatch[raster_ft][raster_fn]
            height, width = raster.shape[:2] if raster_ft.is_timeless() else raster.shape[1: 3]

            if self.raster_dtype:
                raster = raster.astype(self.raster_dtype)

            data_transform = rasterio.transform.from_bounds(*eopatch.bbox, width=width, height=height)

            crs = eopatch.bbox.get_crs()

            if raster_ft.is_timeless():
                eopatch[vector_ft][vector_fn] = self._vectorize_single_raster(raster, data_transform, crs)
            else:
                gpd_list = [self._vectorize_single_raster(raster[time_idx, ...], data_transform, crs,
                                                          timestamp=eopatch.timestamp[time_idx])
                            for time_idx in range(raster.shape[0])]

                eopatch[vector_ft][vector_fn] = GeoDataFrame(pandas.concat(gpd_list, ignore_index=True),
                                                             crs=gpd_list[0].crs)

        return eopatch
