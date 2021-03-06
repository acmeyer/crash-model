import argparse
import osmnx as ox
import fiona
import shutil
import os
import re
import csv
import geojson
import json
import requests
import yaml
import sys

MAP_FP = None


def find_osm_polygon(city):
    """Interrogate the OSM nominatim API for a city polygon.

    Nominatim may not always return city matches in the most intuitive order,
    so results need to be searched for a compatible polygon. The index of the
    polygon is required for proper use of osmnx.graph_from_place(). Some cities
    do not have a polygon at all, in which case they defer to using
    graph_from_point() with city lat & lng.

    Args:
        city (str): city to search for
    Returns:
        int: index of polygon+1 (becomes the correct 'which_result' value)
        None: if no polygon found
    """

    search_params = {'format': 'json', 'limit': 5,
                     'dedupe': 0, 'polygon_geojson': 1, 'q': city}
    url = 'https://nominatim.openstreetmap.org/search'

    response = requests.get(url, params=search_params)

    for index, match in enumerate(response.json()):
        # a match that can be used by graph_from_place needs to be a Polygon
        # or MultiPolygon
        if (match['geojson']['type'] in ['Polygon', 'MultiPolygon']):
            return index+1

    return None


def simple_get_roads(config):
    """
    Use osmnx to get a simplified version of open street maps for the city
    Writes osm_nodes and osm_ways shapefiles to MAP_FP
    Args:
        city
    Returns:
        None, but creates the following shape files:
           osm_ways.shp - the simplified road network
           osm_nodes.shp - the intersections and dead ends
        And creates the following directory:
           all_nodes - containing edges and nodes directories
               for the unsimplified road network
    """

    # confirm if a polygon is available for this city, which determines which
    # graph function is appropriate
    print("searching nominatim for " + str(config['city']) + " polygon")
    polygon_pos = find_osm_polygon(config['city'])

    if (polygon_pos is not None):
        print("city polygon found in OpenStreetMaps at position " +
              str(polygon_pos) + ", building graph of roads within " +
              "specified bounds")
        G1 = ox.graph_from_place(config['city'], network_type='drive',
                                 simplify=False, which_result=polygon_pos)

    else:
        # City & lat+lng+radius required from config to graph from point
        if ('city' not in list(config.keys()) or config['city'] is None):
            sys.exit('city is required in config file')

        if ('city_latitude' not in list(config.keys()) or
                config['city_latitude'] is None):
            sys.exit('city_latitude is required in config file')

        if ('city_longitude' not in list(config.keys()) or
                config['city_longitude'] is None):
            sys.exit('city_longitude is required in config file')

        if ('city_radius' not in list(config.keys()) or
                config['city_radius'] is None):
            sys.exit('city_radius is required in config file')

        print("no city polygon found in OpenStreetMaps, building graph of " +
              "roads within " + str(config['city_radius']) + "km of city " +
              str(config['city_latitude']) + " / " +
              str(config['city_longitude']))
        G1 = ox.graph_from_point((config['city_latitude'],
                                  config['city_longitude']),
                                 distance=config['city_radius'] * 1000,
                                 network_type='drive', simplify=False)

    G = ox.simplify_graph(G1)

    # Label endpoints
    streets_per_node = ox.count_streets_per_node(G)
    for node, count in list(streets_per_node.items()):
        if count <= 1:
            G.nodes()[node]['dead_end'] = True

    # osmnx creates a directory for the nodes and edges
    # Store all nodes, since they can be other features
    ox.save_graph_shapefile(
        G1, filename='all_nodes', folder=MAP_FP)

    # Store simplified network
    ox.save_graph_shapefile(
        G, filename='temp', folder=MAP_FP)

    # Copy and remove temp directory
    tempdir = os.path.join(MAP_FP, 'temp')
    for filename in os.listdir(os.path.join(tempdir, 'edges')):
        name, extension = filename.split('.')
        shutil.move(os.path.join(tempdir, 'edges', filename),
                    os.path.join(MAP_FP, 'osm_ways.' + extension))
    for filename in os.listdir(os.path.join(tempdir, 'nodes')):
        name, extension = filename.split('.')
        shutil.move(os.path.join(tempdir, 'nodes', filename),
                    os.path.join(MAP_FP, 'osm_nodes.' + extension))
    shutil.rmtree(tempdir)


def clean_and_write(ways_file, nodes_file,
                    result_file, DOC_FP):
    """
    Takes several shape files in 4326 projection, created from osmnx,
    reprojects them, and calls write_geojson
    Args:
        ways_file - shp file for the ways
        nodes_file - shp file for the intersection and end nodes
        all_nodes_file - shp file for ALL nodes in the road network
        result_file - file to write to
        DOC_FP - file to write highway keys to
    Returns:
        None, writes a geojson file
    """
    cleaned_ways = clean_ways(ways_file, DOC_FP)
    nodes = fiona.open(nodes_file)
    nodes, cleaned_ways = get_connections(cleaned_ways, nodes)
    write_geojson(cleaned_ways, nodes,
                  result_file)


def get_connections(ways, nodes):
    """
    Populate the cross streets for each node,
    and add unique ids to the ways
    Args:
        ways - a list of geojson linestrings
        nodes - a list of geojson points
    Returns:
        nodes - a dict containing the roads connected to each node
        ways - the ways, with a unique osmid-fromnode-to-node string
    """

    node_info = {}
    for way in ways:
        # There are some collector roads and others that don't
        # have names. Skip these
        if way['properties']['name']:

            # While we are still merging segments with different names,
            # just use both roads. This should be revisited
            if '[' in way['properties']['name']:
                way['properties']['name'] = re.sub(
                    r'[^\s\w,]|_', '', way['properties']['name'])
                way['properties']['name'] = "/".join(
                    way['properties']['name'].split(', '))

            if way['properties']['from'] not in node_info.keys():
                node_info[way['properties']['from']] = []
            node_info[way['properties']['from']].append(
                way['properties']['name'])

            if way['properties']['to'] not in node_info.keys():
                node_info[way['properties']['to']] = []
            node_info[way['properties']['to']].append(
                way['properties']['name'])

        ident = str(way['properties']['osmid']) + '-' \
            + str(way['properties']['from']) + '-' \
            + str(way['properties']['to'])
        way['properties']['segment_id'] = ident

    nodes_with_streets = []
    for node in nodes:
        if node['properties']['osmid'] in node_info:
            node['properties']['streets'] = ', '.join(
                set(node_info[node['properties']['osmid']]))
        else:
            node['properties']['streets'] = ''
        nodes_with_streets.append(node)
    return nodes_with_streets, ways


def write_highway_keys(DOC_FP, highway_keys):
    """
    Since we're creating a numeric highway key, we'd like to know what
    the numbers correspond to, so write to file the mapping from key
    to open street map highway type
    Args:
        DOC_FP - the directory to write the file
        highway_keys - a dict associating key with string type
    """
    # Write highway keys to docs if needed for reference
    if not os.path.exists(DOC_FP):
        os.makedirs(DOC_FP)
    with open(os.path.join(DOC_FP, 'highway_keys.csv'), 'w') as f:
        w = csv.writer(f)
        w.writerow(['type', 'value'])
        for item in highway_keys.items():
            w.writerow(item)


def clean_ways(orig_file, DOC_FP):
    """
    Reads in osm_ways file, cleans up the features, and reprojects
    results into 3857 projection
    Additionally writes a key which shows the correspondence between
    highway type as a string and the resulting int feature
    Features:
        width
        lanes
        hwy_type
        osm_speed
        signal
    Args:
        orig_file: Filename for original file
        result_file: Filename for resulting file in 3857 projection
        DOC_FP: directory to write highway keys file to
    Returns:
        a list of reprojected way lines
    """

    way_lines = fiona.open(orig_file)

    highway_keys = {}
    results = []
    for way_line in way_lines:

        # All features need to be ints, so convert them here

        # Use speed limit if given in osm
        speed = way_line['properties']['maxspeed']
        if speed:
            s = re.search('[0-9]+', speed)
            if s:
                speed = s.group(0)
        if not speed:
            speed = 0

        # round width
        width = 0
        if ['width'] in list(way_line['properties'].keys()):
            width = way_line['properties']['width']
            if not width or ';' in width or '[' in width:
                width = 0
            else:
                width = round(float(width))

        lanes = way_line['properties']['lanes']
        if lanes:
            lanes = max([int(x) for x in re.findall('\d', lanes)])
        else:
            lanes = 0

        # Need to have an int highway field
        if way_line['properties']['highway'] not in list(highway_keys.keys()):
            highway_keys[way_line['properties']['highway']] = len(highway_keys)

        # Use oneway
        oneway = 0
        if way_line['properties']['oneway'] == 'True':
            oneway = 1

        way_line['properties'].update({
            'width': width,
            'lanes': int(lanes),
            'hwy_type': highway_keys[way_line['properties']['highway']],
            'osm_speed': speed,
            'signal': 0,
            'oneway': oneway
        })
        results.append(way_line)

    write_highway_keys(DOC_FP, highway_keys)
    return results


def write_geojson(way_results, node_results, outfp):
    """
    Given a list of ways, intersection nodes, and all nodes, write them
    out to a geojson file.
    """
    feats = way_results

    for node in node_results:
        if not node['properties']['dead_end']:
            node['properties']['intersection'] = 1
        if node['properties']['highway'] == 'traffic_signals':
            node['properties']['signal'] = 1
        feats.append(geojson.Feature(
            geometry=geojson.Point(node['geometry']['coordinates']),
            properties=node['properties'])
        )

    feat_collection = geojson.FeatureCollection(feats)
    with open(outfp, 'w') as outfile:
        geojson.dump(feat_collection, outfile)


def write_features(all_nodes_file):
    """
    Adds relevant features (at this time, only point-based)
    from open street maps
    """

    all_node_results = fiona.open(all_nodes_file)

    features = []
    # Go through the rest of the nodes, and add any of them that have
    # (hardcoded) open street map features that we care about
    # For the moment, all_nodes only contains street nodes, so we'll
    # only look at signals and crosswalks
    for node in all_node_results:
        if node['properties']['highway'] == 'crossing':
            features.append(geojson.Feature(
                geometry=geojson.Point(node['geometry']['coordinates']),
                id=node['properties']['osmid'],
                properties={'feature': 'crosswalk'},
            ))

        elif node['properties']['highway'] == 'traffic_signals':
            features.append(geojson.Feature(
                geometry=geojson.Point(node['geometry']['coordinates']),
                id=node['properties']['osmid'],
                properties={'feature': 'signal'},
            ))

    features = geojson.FeatureCollection(features)

    with open(os.path.join(MAP_FP, 'features.geojson'), "w") as f:
        json.dump(features, f)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--config", type=str, required=True,
                        help="Config file")
    parser.add_argument("-d", "--datadir", type=str, required=True,
                        help="Data directory")
    # Can force update
    parser.add_argument('--forceupdate', action='store_true',
                        help='Whether to force update the maps')

    args = parser.parse_args()

    config_file = args.config
    with open(config_file) as f:
        config = yaml.safe_load(f)

    MAP_FP = os.path.join(args.datadir, 'processed/maps')
    DOC_FP = os.path.join(args.datadir, 'docs')

    # If maps do not exist, create
    if not os.path.exists(os.path.join(MAP_FP, 'osm_ways.shp')) \
       or args.forceupdate:
        print('Generating map from open street map...')
        simple_get_roads(config)

    if not os.path.exists(os.path.join(MAP_FP, 'osm_elements.geojson')) \
       or args.forceupdate:
        print("Cleaning and writing to {}...".format('osm_elements.geojson'))

        clean_and_write(
            os.path.join(MAP_FP, 'osm_ways.shp'),
            os.path.join(MAP_FP, 'osm_nodes.shp'),
            os.path.join(MAP_FP, 'osm_elements.geojson'),
            DOC_FP
        )

    write_features(os.path.join(MAP_FP, 'all_nodes', 'nodes', 'nodes.shp'))
