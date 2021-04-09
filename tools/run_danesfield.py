#!/usr/bin/env python

###############################################################################
# Copyright Kitware Inc. and Contributors
# Distributed under the Apache License, 2.0 (apache.org/licenses/LICENSE-2.0)
# See accompanying Copyright.txt and LICENSE files for details
###############################################################################


"""
Run the Danesfield processing pipeline on an AOI from start to finish.
"""

import argparse
import configparser
import datetime
import glob
import logging
import os
import re
import subprocess
from pathlib import Path
import sys
import itertools
import json


def create_working_dir(working_dir, imagery_dir):
    """
    Create working directory for running algorithms
    All files generated by the system are written to this directory.

    :param working_dir: Directory to create for work. Cannot be a subdirectory of `imagery_dir`.
    This is to avoid adding the work images to the pipeline when traversing the `imagery_dir`.
    :type working_dir: str

    :param imagery_dir: Directory where imagery is stored.
    :type imagery_dir: str

    :raises ValueError: If `working_dir` is a subdirectory of `imagery_dir`.
    """
    if not working_dir:
        date_str = str(datetime.datetime.now().timestamp())
        working_dir = 'danesfield-' + date_str.split('.')[0]
    if not os.path.isdir(working_dir):
        os.mkdir(working_dir)
    if os.path.realpath(imagery_dir) in os.path.realpath(working_dir):
        raise ValueError('The working directory ({}) is a subdirectory of the imagery directory '
                         '({}).'.format(working_dir, imagery_dir))
    return working_dir


def ensure_complete_modality(modality_dict, require_rpc=False):
    """
    Ensures that a certain modality (MSI, PAN, SWIR) has all of the required files for computation
    through the whole pipeline.

    :param modality_dict: Mapping of a certain modality to its image, rpc, and info files.
    :type modality_dict: dict

    :param require_rpc: Whether or not to consider the rpc file being present as a requirement for
    a complete modality.
    :type require_rpc: bool
    """
    keys = ['image', 'info']
    if require_rpc:
        keys.append('rpc')
    return all(key in modality_dict for key in keys)


def collate_input_paths(paths):
    """
    Collate a list of input file paths into a dictionary.  Considers
    the files identifier, modality, and extension.

    :param paths: List of input files paths to collate
    :type paths: enumerable
    """
    input_re = re.compile(r'(?P<gra>GRA_)?.*'
                          '(?P<prefix>[0-9]{2}[A-Z]{3}[0-9]{8})\-'
                          '(?P<modality>P1BS|M1BS|A1BS)\-'
                          '(?P<trail>[0-9]{12}_[0-9]{2}_P[0-9]{3}).*'
                          '(?P<ext>\..+)$')

    modality_map = {'P1BS': 'pan',
                    'M1BS': 'msi',
                    'A1BS': 'swir'}

    out_collection = {}
    for path in paths:
        # Match on upper-case path
        match = input_re.match(os.path.basename(path).upper())
        if match:
            key = '%s-%s' % (match.group('prefix'), match.group('trail'))
            modality = modality_map[match.group('modality')]
            if key not in out_collection:
                out_collection[key] = {modality: {}}
            elif modality not in out_collection[key]:
                out_collection[key][modality] = {}

            if match.group('gra') is not None and \
               match.group('ext').endswith('.RPC'):
                out_collection[key][modality]['rpc'] = path
            elif match.group('ext').endswith('.NTF'):
                out_collection[key][modality]['image'] = path
            elif match.group('ext').endswith('.TAR'):
                out_collection[key][modality]['info'] = path

    return out_collection


# Get path to tool relative to this (run_danesfield.py)
def relative_tool_path(rel_path):
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), rel_path)


# Helper for building the python command.  The '-u' option tells
# python not to buffer IO
def py_cmd(tool_path):
    return ['python', '-u', tool_path]


def run_step(working_dir, step_name, command, abort_on_error=True):
    """
    Runs a command if it has not already been run succcessfully.  Log
    and exit status files are written to `working_dir`.  This script
    will exit(1) if the command's exit status is anything but 0, and
    if `abort_on_error` is True.

    The stdout and stderr of the command are both printed to stdout
    and written to the log file.

    :param working_dir: Directory to create for log and exit status
    output files.
    :type working_dir: str

    :param step_name: Nominal identifier for the step.
    :type step_name: str

    :param command: Command passed directly to `subprocess.Popen`.
    :type command: array or str

    :param abort_on_error: If True, the program will exit if the step
    fails.  Default is True.
    :type abort_on_error: bool
    """
    # Path to the log file, which will include both the stdout and
    # stderr from the step command
    step_log_fpath = os.path.join(working_dir, '{}.log'.format(step_name))
    # Empty file to indicate the exit status (return code) of the step
    # command.  The exit status is appended to this prefix before
    # creation
    step_returncode_fpath_prefix = os.path.join(working_dir,
                                                '{}.exitstatus'.format(step_name))

    # Check that we haven't already succcessfully completed this step
    # (as indicated by an exit status of 0)
    if os.path.isfile('{}.0'.format(step_returncode_fpath_prefix)):
        return 0
    else:
        # If we haven't run the step, or a previous run failed, remove
        # previous log and returncode files
        if os.path.isfile(step_log_fpath):
            os.remove(step_log_fpath)
        for f in glob.glob('{}.*'.format(step_returncode_fpath_prefix)):
            os.remove(f)

        # Create step working directory if it didn't already exist
        if not os.path.isdir(working_dir):
            os.mkdir(working_dir)

        logging.info("---- Running step: {} ----".format(step_name))
        logging.debug(command)
        # Run the step; newline buffered text
        proc = subprocess.Popen(command,
                                stderr=subprocess.STDOUT,
                                stdout=subprocess.PIPE,
                                universal_newlines=True,
                                bufsize=1)

        # Write the output/err both to stdout and the log file
        with open(step_log_fpath, 'w') as out_f:
            for line in proc.stdout:
                print(line, end='')
                print(line, end='', file=out_f)

        # Wait for the process to terminate and set the return code
        # (max of 5 seconds)
        proc.wait(timeout=5)
        Path('{}.{}'.format(step_returncode_fpath_prefix, proc.returncode)).touch()

        if abort_on_error and proc.returncode != 0:
            logging.error('---- Error on step: {}.  Aborting! ----'.format(step_name))
            exit(1)

        return proc.returncode


def main(args):
    parser = argparse.ArgumentParser(
        description="Run the Danesfield processing pipeline on an AOI from start to finish.")
    parser.add_argument("ini_file",
                        help="ini file")
    parser.add_argument("--vissat", help="run Vissat stereo pipeline", action="store_true")
    parser.add_argument("--run_metrics", help="run metrics", action="store_true")
    args = parser.parse_args(args)

    # Read configuration file
    config = configparser.ConfigParser()
    config.read(args.ini_file)

    # This either parses the working directory from the configuration file and passes it to
    # create the working directory or passes None and so some default working directory is
    # created (based on the time of creation)
    working_dir = create_working_dir(config['paths'].get('work_dir'),
                                     config['paths']['imagery_dir'])

    aoi_name = config['aoi']['name']

    gsd = float(config['params'].get('gsd', 0.25))

    #############################################
    # Run P3D point cloud generation
    #############################################
    # This script assumes we already have a pointcloud generated from
    # Raytheon's P3D.  See the README for information regarding P3D.

    p3d_file = config['paths']['p3d_fpath']

    #############################################
    # Run VisSat pipeline
    #############################################
    if args.vissat:
        aoi_config = config['paths'].get('aoi_config')
        if aoi_config == None:
            print("Error: Path to aoi_config file must be provided when using VisSat")
            exit(1)
        with open(aoi_config) as f:
            data = json.load(f)

        vissat_workdir = data['work_dir']
        utm = config['aoi'].get('utm')
        if utm == None:
            print("Error: UTM zone must be provided when using VisSat")
            exit(1)
        cmd_args = ["python3", "/VisSatSatelliteStereo/stereo_pipeline.py", "--config_file", aoi_config]
        run_step(vissat_workdir, "VisSat", cmd_args)
        cmd_args = ["python3", "/ply2txt.py", os.path.join(vissat_workdir, 
                    'mvs_results/aggregate_3d/aggregate_3d.ply'), 
                    os.path.join(vissat_workdir, 'mvs_results/aggregate_3d/aggregate_3d.txt')]

        run_step(vissat_workdir, "ply2txt", cmd_args)
        cmd_args = ["/LAStools/bin/txt2las", "-i", os.path.join(vissat_workdir,
                    'mvs_results/aggregate_3d/aggregate_3d.txt'), 
                    "-parse", "xyz", "-o", p3d_file, "-utm", utm, 
                    "-target_utm", utm]
                    
        run_step(vissat_workdir, "txt2las", cmd_args)

    #############################################
    # Find all NTF and corresponding rpc and info
    # tar files
    #############################################

    input_paths = []
    use_rpcs = (config['paths'].get('rpc_dir')!=None)
    if use_rpcs:
        iterable = itertools.chain(os.walk(config['paths']['imagery_dir']), 
                                   os.walk(config['paths']['rpc_dir']))
    else:
        iterable = os.walk(config['paths']['imagery_dir'])
    for root, dirs, files in iterable:
        input_paths.extend([os.path.join(root, f) for f in files])

    collection_id_to_files = collate_input_paths(input_paths)

    # Prune the collection
    incomplete_ids = []
    for prefix, files in collection_id_to_files.items():
        if 'msi' in files and ensure_complete_modality(files['msi'], require_rpc=use_rpcs) and \
           'pan' in files and ensure_complete_modality(files['pan'], require_rpc=use_rpcs):
            pass
        else:
            logging.warning("Don't have complete modality for collection ID: '{}', skipping!"
                            .format(prefix))
            incomplete_ids.append(prefix)

    for idx in incomplete_ids:
        del collection_id_to_files[idx]

    #############################################
    # Render DSM from P3D point cloud
    #############################################

    generate_dsm_outdir = os.path.join(working_dir, 'generate-dsm')
    dsm_file = os.path.join(generate_dsm_outdir, aoi_name + '_P3D_DSM.tif')

    cmd_args = py_cmd(relative_tool_path('generate_dsm.py'))
    cmd_args += [dsm_file, '-s', p3d_file]
    cmd_args += ['--gsd', str(gsd)]

    bounds = config['aoi'].get('bounds')
    if bounds:
        cmd_args += ['--bounds']
        cmd_args += bounds.split(' ')

    run_step(generate_dsm_outdir,
             'generate-dsm',
             cmd_args)

    # #############################################
    # # Fit Dtm to the DSM
    # #############################################

    fit_dtm_outdir = os.path.join(working_dir, 'fit-dtm')
    dtm_file = os.path.join(fit_dtm_outdir, aoi_name + '_DTM.tif')

    cmd_args = py_cmd(relative_tool_path('fit_dtm.py'))
    cmd_args += [dsm_file, dtm_file]

    run_step(fit_dtm_outdir,
             'fit-dtm',
             cmd_args)

    #############################################
    # Orthorectify images
    #############################################
    # For each MSI source image call orthorectify.py
    # needs to use the DSM, DTM from above and Raytheon RPC file,
    # which is a by-product of P3D.

    orthorectify_outdir = os.path.join(working_dir, 'orthorectify')
    for collection_id, files in collection_id_to_files.items():
        # Orthorectify the msi images
        msi_ntf_fpath = files['msi']['image']
        msi_fname = os.path.splitext(os.path.split(msi_ntf_fpath)[1])[0]
        msi_ortho_img_fpath = os.path.join(orthorectify_outdir, '{}_ortho.tif'.format(msi_fname))
        cmd_args = py_cmd(relative_tool_path('orthorectify.py'))
        cmd_args += [msi_ntf_fpath, dsm_file, msi_ortho_img_fpath, '--dtm', dtm_file]

        msi_rpc_fpath = files['msi'].get('rpc', None)
        if msi_rpc_fpath:
            cmd_args.extend(['--raytheon-rpc', msi_rpc_fpath])

        run_step(orthorectify_outdir,
                 'orthorectify-{}'.format(msi_fname),
                 cmd_args)

        files['msi']['ortho_img_fpath'] = msi_ortho_img_fpath
    #
    # Note: we may eventually select a subset of input images
    # on which to run this and the following steps

    #############################################
    # Compute NDVI
    #############################################
    # Compute the NDVI from the orthorectified / pansharpened images
    # for use during segmentation

    ndvi_outdir = os.path.join(working_dir, 'compute-ndvi')
    ndvi_output_fpath = os.path.join(ndvi_outdir, 'ndvi.tif')
    cmd_args = py_cmd(relative_tool_path('compute_ndvi.py'))
    cmd_args += [files['msi']['ortho_img_fpath'] for
                 files in
                 collection_id_to_files.values() if
                 'msi' in files and 'ortho_img_fpath' in files['msi']]
    cmd_args.append(ndvi_output_fpath)

    run_step(ndvi_outdir,
             'compute-ndvi',
             cmd_args)

    #############################################
    # Get OSM road vector data
    #############################################
    # Query OpenStreetMap for road vector data

    get_road_vector_outdir = os.path.join(working_dir, 'get-road-vector')
    road_vector_output_fpath = os.path.join(get_road_vector_outdir, 'road_vector.geojson')
    cmd_args = py_cmd(relative_tool_path('get_road_vector.py'))
    cmd_args += ['--bounding-img', dsm_file,
                 '--output-dir', get_road_vector_outdir]

    run_step(get_road_vector_outdir,
             'get-road-vector',
             cmd_args)

    #############################################
    # Segment by Height and Vegetation
    #############################################
    # Call segment_by_height.py using the DSM, DTM, and NDVI.  the
    # output here has the suffix _threshold_CLS.tif.

    seg_by_height_outdir = os.path.join(working_dir, 'segment-by-height')
    threshold_output_mask_fpath = os.path.join(seg_by_height_outdir, 'threshold_CLS.tif')
    cmd_args = py_cmd(relative_tool_path('segment_by_height.py'))
    cmd_args += [dsm_file,
                 dtm_file,
                 threshold_output_mask_fpath,
                 '--input-ndvi', ndvi_output_fpath,
                 '--road-vector', road_vector_output_fpath,
                 '--road-rasterized',
                 os.path.join(seg_by_height_outdir, 'road_rasterized.tif'),
                 '--road-rasterized-bridge',
                 os.path.join(seg_by_height_outdir, 'road_rasterized_bridge.tif')]

    run_step(seg_by_height_outdir,
             'segment-by-height',
             cmd_args)

    #############################################
    # Material Segmentation
    #############################################

    material_classifier_outdir = os.path.join(working_dir, 'material-classification')
    cmd_args = py_cmd(relative_tool_path('material_classifier.py'))
    cmd_args += ['--image_paths']
    # We build these up separately because they have to be 1-to-1 on the command line and
    # dictionaries are unordered
    img_paths = []
    info_paths = []
    for collection_id, files in collection_id_to_files.items():
        img_paths.append(files['msi']['ortho_img_fpath'])
        info_paths.append(files['msi']['info'])
    cmd_args.extend(img_paths)
    cmd_args.append('--info_paths')
    cmd_args.extend(info_paths)
    cmd_args.extend(['--output_dir', material_classifier_outdir,
                     '--model_path', config['material']['model_fpath'],
                     '--outfile_prefix', aoi_name])
    if config.has_option('material', 'batch_size'):
        cmd_args.extend(['--batch_size', config.get('material', 'batch_size')])
    if config['material'].getboolean('cuda'):
            cmd_args.append('--cuda')

    run_step(material_classifier_outdir,
             'material-classification',
             cmd_args)

    #############################################
    # Roof Geon Extraction & PointNet Geon Extraction
    #############################################
    # This script encapsulates both Columbia's and Purdue's components
    # for roof segmentation and geon extraction / reconstruction
    # Output files are named building_<N>.obj and building_<N>.json where <N> is
    # a integer, starting at 0.

    roof_geon_extraction_outdir = os.path.join(working_dir, 'roof-geon-extraction')
    cmd_args = py_cmd(relative_tool_path('roof_geon_extraction.py'))
    cmd_args += [
        '--las', p3d_file,
        # Note that we're currently using the CLS file from the
        # segment by height script
        '--cls', threshold_output_mask_fpath,
        '--dtm', dtm_file,
        '--model_dir', config['roof']['model_dir'],
        '--model_prefix', config['roof']['model_prefix'],
        '--output_dir', roof_geon_extraction_outdir
    ]

    run_step(roof_geon_extraction_outdir,
             'roof-geon-extraction',
             cmd_args)

    #############################################
    # Texture Mapping
    #############################################

    crop_and_pansharpen_outdir = os.path.join(working_dir, 'crop-and-pansharpen')
    for collection_id, files in collection_id_to_files.items():
        cmd_args = py_cmd(relative_tool_path('crop_and_pansharpen.py'))
        cmd_args += [dsm_file, crop_and_pansharpen_outdir, "--pan", files['pan']['image']]
        rpc_fpath = files['pan'].get('rpc', None)
        if (rpc_fpath):
            cmd_args.append(rpc_fpath)
        cmd_args.extend(["--msi", files['msi']['image']])
        rpc_fpath = files['msi'].get('rpc', None)
        if (rpc_fpath):
            cmd_args.append(rpc_fpath)

        run_step(crop_and_pansharpen_outdir,
                 'crop-and-pansharpen-{}'.format(collection_id),
                 cmd_args)

    texture_mapping_outdir = os.path.join(working_dir, 'texture-mapping')
    occlusion_mesh = "xxxx.obj"
    images_to_use = glob.glob(os.path.join(crop_and_pansharpen_outdir,
                                           "*_crop_pansharpened_processed.tif"))
    orig_meshes = glob.glob(os.path.join(roof_geon_extraction_outdir, "*.obj"))

    orig_meshes = [e for e in orig_meshes
                   if e.find(occlusion_mesh) < 0]

    cmd_args = py_cmd(relative_tool_path('texture_mapping.py'))
    cmd_args += [dsm_file, dtm_file, texture_mapping_outdir, occlusion_mesh, "--crops"]
    cmd_args.extend(images_to_use)
    cmd_args.append("--buildings")
    cmd_args.extend(orig_meshes)

    run_step(texture_mapping_outdir,
             'texture-mapping',
             cmd_args)

    #############################################
    # Buildings to DSM
    #############################################
    roof_geon_extraction_outdir = os.path.join(working_dir, 'roof-geon-extraction')

    buildings_to_dsm_outdir = os.path.join(working_dir, 'buildings-to-dsm')
    # Generate the output DSM
    output_dsm = os.path.join(buildings_to_dsm_outdir, "buildings_to_dsm_DSM.tif")
    cmd_args = py_cmd(relative_tool_path('buildings_to_dsm.py'))
    cmd_args += [dtm_file,
                 output_dsm]
    cmd_args.append('--input_obj_paths')
    obj_list = glob.glob("{}/*.obj".format(roof_geon_extraction_outdir))
    # remove occlusion_mesh and results (building_<i>.obj)
    #obj_list = [e for e in obj_list
    #            if e.find(occlusion_mesh) < 0 and e.find("building_") < 0]
    cmd_args.extend(obj_list)

    run_step(buildings_to_dsm_outdir,
             'buildings-to-dsm_DSM',
             cmd_args)

    # Generate the output CLS
    output_cls = os.path.join(buildings_to_dsm_outdir, "buildings_to_dsm_CLS.tif")
    cmd_args = py_cmd(relative_tool_path('buildings_to_dsm.py'))
    cmd_args += [dtm_file,
                 output_cls,
                 '--render_cls']
    cmd_args.append('--input_obj_paths')
    cmd_args.extend(obj_list)

    run_step(buildings_to_dsm_outdir,
             'buildings-to-dsm_CLS',
             cmd_args)

    
    #############################################
    # Run metrics
    #############################################
    if(args.run_metrics):
        run_metrics_outdir = os.path.join(working_dir, 'run_metrics')

        # Expected file path for material classification output MTL file
        output_mtl = os.path.join(material_classifier_outdir, '{}_MTL.tif'.format(aoi_name))

        cmd_args = py_cmd(relative_tool_path('run_metrics.py'))
        cmd_args += [
            '--output-dir', run_metrics_outdir,
            '--ref-dir', config['metrics']['ref_data_dir'],
            '--ref-prefix', config['metrics']['ref_data_prefix'],
            '--dsm', output_dsm,
            '--cls', output_cls,
            '--mtl', output_mtl,
            '--dtm', dtm_file]

        run_step(run_metrics_outdir,
                'run-metrics',
                cmd_args)
    


if __name__ == '__main__':
    loglevel = os.environ.get('LOGLEVEL', 'INFO').upper()
    logging.basicConfig(level=loglevel)

    main(sys.argv[1:])
