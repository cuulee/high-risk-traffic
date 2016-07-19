#!/usr/bin/env python

"""Loads records and roads shapefile, outputs data needed for training"""
import argparse
import csv
from dateutil import parser
from dateutil.relativedelta import relativedelta
import fiona
from functools import partial
import itertools
import logging
from math import ceil
import multiprocessing
import os
import pyproj
import pytz
import rtree
from shapely.geometry import mapping, shape, LineString, MultiPoint, Point
from shapely.ops import transform, unary_union


logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger()


def should_keep_road(road, road_shp, record_buffers_index):
    """Returns true if road should be considered for segmentation
    :param road: Dictionary representation of the road (with properties)
    :param roads_shp: Shapely representation of the road
    :param record_buffers_index: RTree index of the record_buffers
    """
    # If the road has no nearby records, then we can discard it early on.
    # This provides a major optimization since the majority of roads don't have recorded accidents.
    if not len(list(record_buffers_index.intersection(road_shp.bounds))):
        return False

    if ('highway' in road['properties']
            and road['properties']['highway'] is not None
            and road['properties']['highway'] != 'path'
            and road['properties']['highway'] != 'footway'):
        return True
    # We're only interested in non-bridge, non-tunnel highways
    # 'class' is optional, so only consider it when it's available.
    if ('class' not in road['properties'] or road['properties']['class'] == 'highway'
            and road['properties']['bridge'] == 0
            and road['properties']['tunnel'] == 0):
        return True
    return False


def read_roads(roads_shp, records, buffer_size):
    """Reads shapefile and extracts roads and projection
    :param roads_shp: Path to the shapefile containing roads
    :param records: List of shapely geometries representing record points
    :param buffer_size: Number of units to buffer record for checking if road should be kept
    """
    # Create a spatial index for record buffers to efficiently find intersecting roads
    record_buffers_index = rtree.index.Index()
    for idx, record in enumerate(records):
        record_point = record['point']
        record_buffer_bounds = record_point.buffer(buffer_size).bounds
        record_buffers_index.insert(idx, record_buffer_bounds)

    shp_file = fiona.open(roads_shp)
    roads = []
    logger.info('Number of total roads in shapefile: {:,}'.format(len(shp_file)))
    for road in shp_file:
        road_shp = shape(road['geometry'])
        if should_keep_road(road, road_shp, record_buffers_index):
            roads.append(road_shp)

    return (roads, shp_file.bounds)


def get_intersections(roads):
    """Calculates the intersection points of all roads
    :param roads: List of shapely geometries representing road segments
    """
    intersections = []
    for road1, road2 in itertools.combinations(roads, 2):
        if road1.intersects(road2):
            intersection = road1.intersection(road2)
            if 'Point' == intersection.type:
                intersections.append(intersection)
            elif 'MultiPoint' == intersection.type:
                intersections.extend([pt for pt in intersection])
            elif 'MultiLineString' == intersection.type:
                multiLine = [line for line in intersection]
                first_coords = multiLine[0].coords[0]
                last_coords = multiLine[len(multiLine)-1].coords[1]
                intersections.append(Point(first_coords[0], first_coords[1]))
                intersections.append(Point(last_coords[0], last_coords[1]))
            elif 'GeometryCollection' == intersection.type:
                intersections.extend(get_intersections(intersection))

    # The unary_union removes duplicate points
    unioned = unary_union(intersections)

    # Ensure the result is a MultiPoint, since calling functions expect an iterable
    if 'Point' == unioned.type:
        unioned = MultiPoint([unioned])

    return unioned


def get_intersection_buffers(roads, road_bounds, intersection_buffer_units, tile_max_units):
    """Buffers all intersections
    :param roads: List of shapely geometries representing road segments
    :param road_bounds: Bounding box of the roads shapefile
    :param intersection_buffer_units: Number of units to use for buffer radius
    :param tile_max_units: Maxium number of units for each side of a tile
    """
    # As an optimization, the road network is divided up into a grid of tiles,
    # and intersections are calculated within each tile.
    def roads_per_tile_iter():
        """Generator which yields a set of roads for each tile"""
        min_x, min_y, max_x, max_y = road_bounds
        bounds_width = max_x - min_x
        bounds_height = max_y - min_y
        x_divisions = ceil(bounds_width / tile_max_units)
        y_divisions = ceil(bounds_height / tile_max_units)
        tile_width = bounds_width / x_divisions
        tile_height = bounds_height / y_divisions

        # Create a spatial index for roads to efficiently match up roads to tiles
        logger.info('Generating spatial index for intersections')
        roads_index = rtree.index.Index()
        for idx, road in enumerate(roads):
            roads_index.insert(idx, road.bounds)

        logger.info('Number of tiles: {}'.format(int(x_divisions * y_divisions)))
        for x_offset in range(0, int(x_divisions)):
            for y_offset in range(0, int(y_divisions)):
                road_ids_in_tile = roads_index.intersection([
                    min_x + x_offset * tile_width,
                    min_y + y_offset * tile_height,
                    min_x + (1 + x_offset) * tile_width,
                    min_y + (1 + y_offset) * tile_height
                ])
                roads_in_tile = [roads[road_id] for road_id in road_ids_in_tile]
                if len(roads_in_tile) > 1:
                    yield roads_in_tile

    # Allocate one worker per core, and parallelize the discovery of intersections
    pool = multiprocessing.Pool(multiprocessing.cpu_count())
    tile_intersections = pool.imap(get_intersections, roads_per_tile_iter())
    pool.close()
    pool.join()

    logger.info('Buffering intersections')
    # Note: tile_intersections is a list of multipoints (which is a list of points).
    # itertools.chain.from_iterable flattens the list into a list of single points.
    buffered_intersections = [intersection.buffer(intersection_buffer_units)
                              for intersection in itertools.chain.from_iterable(tile_intersections)]

    # If intersection buffers overlap, union them to treat them as one
    logger.info('Performing unary union on buffered intersections')
    return unary_union(buffered_intersections)


def split_line(line, max_line_units):
    """Checks the line's length and splits in half if larger than the configured max
    :param line: Shapely line to be split
    :param max_line_units: The maximum allowed length of the line
    """
    if line.length <= max_line_units:
        return [line]

    half_length = line.length / 2
    coords = list(line.coords)
    for idx, point in enumerate(coords):
        proj_dist = line.project(Point(point))
        if proj_dist == half_length:
            return [LineString(coords[:idx + 1]), LineString(coords[idx:])]

        if proj_dist > half_length:
            mid_point = line.interpolate(half_length)
            head_line = LineString(coords[:idx] + [(mid_point.x, mid_point.y)])
            tail_line = LineString([(mid_point.x, mid_point.y)] + coords[idx:])
            return split_line(head_line, max_line_units) + split_line(tail_line, max_line_units)


def get_intersection_parts(roads, int_buffers, max_line_units):
    """Finds all segments that intersect the buffers, and all that don't
    :param roads: List of shapely geometries representing road segments
    :param int_buffers: List of shapely polygons representing intersection buffers
    """

    # Create a spatial index for intersection buffers to efficiently find intersecting segments
    int_buffers_index = rtree.index.Index()
    for idx, intersection_buffer in enumerate(int_buffers):
        int_buffers_index.insert(idx, intersection_buffer.bounds)

    segments_map = {}
    non_int_lines = []
    for road in roads:
        road_int_buffers = []
        for idx in int_buffers_index.intersection(road.bounds):
            int_buffer = int_buffers[idx]
            if int_buffer.intersects(road):
                if idx not in segments_map:
                    segments_map[idx] = []
                segments_map[idx].append(int_buffer.intersection(road))
                road_int_buffers.append(int_buffer)

        # Collect the non-intersecting segments
        if len(road_int_buffers) > 0:
            diff = road.difference(unary_union(road_int_buffers))
            if 'LineString' == diff.type:
                non_int_lines.append(diff)
            elif 'MultiLineString' == diff.type:
                non_int_lines.extend([line for line in diff])
        else:
            non_int_lines.append(road)

    # Union all lines found within a buffer, treating them as a single unit
    int_multilines = [unary_union(lines) for _, lines in segments_map.items()]

    # Split any long non-intersecting segments. It's not important that they
    # be equal lengths, just that none of them are exceptionally long.
    split_non_int_lines = []
    for line in non_int_lines:
        split_non_int_lines.extend(split_line(line, max_line_units))

    # Return a tuple of intersection multilines and non-intersecting segments
    return (int_multilines, split_non_int_lines)


def read_records(records_csv, road_projection, record_projection, tz, col_id,
                 col_x, col_y, col_occurred):
    """Reads records csv, projects points, and localizes datetimes
    :param records_csv: Path to CSV containing record data
    :param road_projection: Projection CRS for road data
    :param record_projection: Projection CRS for record data
    :param tz: Timezone id for record data
    :param col_id: Record id column name
    :param col_x: Record x-coordinate column name
    :param col_y: Record y-coordinate column name
    :param col_occurred: Record occurred datetime column name
    """

    # Create a function for projecting a point
    project = partial(
        pyproj.transform,
        pyproj.Proj(record_projection),
        pyproj.Proj(road_projection)
    )

    records = []
    min_occurred = None
    max_occurred = None
    with open(records_csv, 'rb') as records_file:
        csv_reader = csv.DictReader(records_file)
        for row in csv_reader:
            # Collect min and max occurred datetimes, as they'll be used later on
            try:
                parsed_dt = parser.parse(row[col_occurred])

                # Localize datetimes that aren't timezone-aware
                occurred = parsed_dt if parsed_dt.tzinfo else tz.localize(parsed_dt)
            except:
                # Skip the record if it has an invalid datetime
                continue

            if not min_occurred or occurred < min_occurred:
                min_occurred = occurred
            if not max_occurred or occurred > max_occurred:
                max_occurred = occurred

            records.append({
                'id': row[col_id],
                'point': transform(project, Point(float(row[col_x]), float(row[col_y]))),
                'occurred': occurred
            })

    return records, min_occurred, max_occurred


def match_records_to_segments(records, combined_segments, match_tolerance):
    """Matches up each record to its nearest segment
    :param records: List of record objects
    :param combined_segments: List of Shapely objects representing road segments (+ intersections)
    :param match_tolerance: Number of units to buffer for checking a record/road match
    """

    # Create a spatial index for segments to efficiently find nearby records
    segments_index = rtree.index.Index()
    for idx, element in enumerate(combined_segments):
        segments_index.insert(idx, element.bounds)

    segments_with_records = {}
    for record in records:
        record_point = record['point']
        # A record won't always be exactly on the line, so buffer the point
        # by the match tolerance units to capture nearby segments
        record_buffer_bounds = record_point.buffer(match_tolerance).bounds
        nearby_segments = segments_index.intersection(record_buffer_bounds)
        segment_id_with_distance = [
            (segment_id, combined_segments[segment_id].distance(record_point))
            for segment_id in nearby_segments
        ]

        if len(segment_id_with_distance):
            nearest = min(segment_id_with_distance, key=lambda tup: tup[1])
            segment_id = nearest[0]
            if segment_id not in segments_with_records:
                segments_with_records[segment_id] = []
            segments_with_records[segment_id].append(record)
    return segments_with_records


def get_segments_with_data(combined_segments, segments_with_records, min_occurred, max_occurred):
    """Adds calculated data to each segment
    :param combined_segments: List of Shapely objects representing road segments (+ intersections)
    :param segments_with_records: List of tuples containing record objects and segments
    :param min_occurred: Minimum occurred date of records
    :param max_occurred:  Maximum occurred date of records
    """

    # Define the schema used for writing to a shapefile (and a csv).
    # The schema is defined here, because we need to add some variable
    # properties to it later on in the function which is dependent on
    # the number of years of data available. It's also good to have it
    # here since the data being generated here needs to conform to this
    # schema, so a future edit will only involve modifying this function.
    schema = {
        'geometry': 'MultiLineString',
        'properties': {
            # Unique identifier for this segment
            'id': 'int',
            # Length of the segment
            'length': 'float',
            # Number of lines in the segment (measure of intersection complexity)
            'lines': 'int',
            # X-coordinate of segment centroid
            'pointx': 'float',
            # Y-coordinate of segment centroid
            'pointy': 'float',
            # Total number of records matched
            'records': 'int'
        }
    }

    # Figure out the number of full years of data we have so we can create offset aggregations.
    # A year is defined here as 52 weeks, in case we eventually want to do week/month aggregations.
    num_years = (max_occurred - min_occurred).days / (52 * 7)

    # Create the set of year ranges
    year_ranges = [
        (max_occurred - relativedelta(years=offset),
         max_occurred - relativedelta(years=(offset + 1)),
         't{}records'.format(offset))
        for offset in range(num_years)
    ]

    # Add fields to the schema for each year range
    for year_range in year_ranges:
        _, _, records_label = year_range
        # Number of records within the offset period
        schema['properties'][records_label] = 'int'

    segments_with_data = []
    for idx, segment in enumerate(combined_segments):
        is_intersection = 'MultiLineString' == segment.type
        records = segments_with_records.get(idx)
        data = {
            'id': idx,
            'length': segment.length,
            'lines': len(segment) if is_intersection else 1,
            'pointx': segment.centroid.x,
            'pointy': segment.centroid.y,
            'records': len(records) if records else 0
        }

        # Add time offset aggregation data
        for year_range in year_ranges:
            max_occurred, min_occurred, records_label = year_range
            if records:
                records_in_range = [
                    record for record in records
                    if min_occurred < record['occurred'] <= max_occurred
                ]
                data[records_label] = len(records_in_range)
            else:
                data[records_label] = 0

        segments_with_data.append((segment, data))

    return (schema, segments_with_data)


def write_segments_shp(segments_shp_path, road_projection, segments_with_data, schema):
    """Writes all segments to shapefile (both intersections and individual segments)
    :param segments_shp_path: Path to shapefile to write
    :param road_projection: Projection of road data
    :param segments_with_data: List of tuples containing segments and segment data
    :param schema: Schema to use for writing shapefile
    """
    with fiona.open(segments_shp_path, 'w', driver='ESRI Shapefile',
                    schema=schema, crs=road_projection) as output:
        for segment_with_data in segments_with_data:
            segment, data = segment_with_data
            output.write({
                'geometry': mapping(segment),
                'properties': data
            })


def write__training_csv(segments_csv_path, segments_with_data, schema):
    """Writes all segments containing record data to csv for training
    :param segments_csv_path: Path to CSV to write
    :param segments_with_data: List of tuples containing segments and segment data
    :param schema: Schema to use for writing CSV
    """
    field_names = sorted(schema['properties'].keys())
    with open(segments_csv_path, 'w') as csv_file:
        csv_writer = csv.DictWriter(csv_file, fieldnames=field_names)
        csv_writer.writeheader()
        for segment_with_data in segments_with_data:
            _, data = segment_with_data
            if data['records'] > 0:
                csv_writer.writerow(data)


def main():
    """Main entry point of script"""
    parser = argparse.ArgumentParser(description='Generate training input')
    # Required arguments
    parser.add_argument('roads_shp', help='Path to shapefile containing OSM road data')
    parser.add_argument('records_csv', help='Path to CSV containing record data')

    # Optional arguments
    parser.add_argument('--output-dir', help='Directory where files are output', default='.')
    parser.add_argument('--combined-segments-shp-name', help='Combined segments output .shp name',
                        default='combined_segments.shp')
    parser.add_argument('--training-csv-name',
                        help='Training input .csv name',
                        default='training_input.csv')
    parser.add_argument('--intersection-buffer-units', help='Units to buffer each intersection',
                        default=5)
    parser.add_argument('--tile-max-units', help='Maximum units for each side of a tile',
                        default=3000)
    parser.add_argument('--max_line_units', help='Maximum units allowed for line segment',
                        default=200)
    parser.add_argument('--time-zone', help='Time zone of records', default='America/New_York')
    parser.add_argument('--match-tolerance', help='Units to buffer when matching records to roads',
                        default=5)
    parser.add_argument('--road-projection', help='Projection id of roads', default='epsg:32718')
    parser.add_argument('--record-projection', help='Projection id of records', default='epsg:4326')
    parser.add_argument('--record-col-id', help='Record column: id', default='CRN')
    parser.add_argument('--record-col-x', help='Record column: x-coordinate', default='LNG')
    parser.add_argument('--record-col-y', help='Record column: y-coordinate', default='LAT')
    parser.add_argument('--record-col-occurred', help='Record column: occurred',
                        default='DATETIME')

    args = parser.parse_args()

    logger.info('Reading records from {}'.format(args.records_csv))
    tz = pytz.timezone(args.time_zone)
    road_projection = {'init': args.road_projection}
    record_projection = {'init': args.record_projection}
    match_tolerance = args.match_tolerance
    records, min_occurred, max_occurred = read_records(
        args.records_csv, road_projection, record_projection, tz,
        args.record_col_id, args.record_col_x, args.record_col_y,
        args.record_col_occurred
    )
    logger.info('Found {:,} records between {} and {}'.format(
        len(records), min_occurred.date(), max_occurred.date())
    )

    logger.info('Reading shapefile from {}'.format(args.roads_shp))
    roads, road_bounds = read_roads(args.roads_shp, records, match_tolerance)
    logger.info('Number of relevant roads in shapefile: {:,}'.format(len(roads)))

    logger.info('Calculating intersections')
    int_buffers = get_intersection_buffers(roads, road_bounds, args.intersection_buffer_units,
                                           args.tile_max_units)

    logger.info('Getting intersection parts')
    int_multilines, non_int_lines = get_intersection_parts(roads, int_buffers, args.max_line_units)
    combined_segments = int_multilines + non_int_lines
    logger.info('Found {:,} intersection multilines'.format(len(int_multilines)))
    logger.info('Found {:,} non-intersection lines'.format(len(non_int_lines)))
    logger.info('Found {:,} combined segments'.format(len(combined_segments)))

    segments_with_records = match_records_to_segments(
        records, combined_segments, match_tolerance)
    logger.info('Found {:,} segments with records'.format(len(segments_with_records)))

    schema, segments_with_data = get_segments_with_data(
        combined_segments, segments_with_records, min_occurred, max_occurred
    )
    logger.info('Compiled data for {:,} segments'.format(len(segments_with_data)))

    segments_shp_path = os.path.join(args.output_dir, args.combined_segments_shp_name)
    write_segments_shp(segments_shp_path, road_projection, segments_with_data, schema)
    logger.info('Generated shapefile')

    training_csv_path = os.path.join(args.output_dir, args.training_csv_name)
    write__training_csv(training_csv_path, segments_with_data, schema)
    logger.info('Generated csv for training')

if __name__ == '__main__':
    main()
