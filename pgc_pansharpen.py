import os, string, sys, shutil, math, glob, re, tarfile, argparse, subprocess, logging
from datetime import datetime, timedelta
import gdal, ogr,osr, gdalconst

from lib import ortho_functions, utils

#### Create Loggers
logger = logging.getLogger("logger")
logger.setLevel(logging.DEBUG)

#### Reg Exs

# WV02_12FEB061315046-P1BS-10300100106FC100.ntf
WV02p = re.compile("WV02_\w+-M")

# WV03_12FEB061315046-P1BS-10300100106FC100.ntf
WV03p = re.compile("WV03_\w+-M")

# QB02_12FEB061315046-P1BS-10300100106FC100.ntf
QB02p = re.compile("QB02_\w+-M")

# GE01_12FEB061315046-P1BS-10300100106FC100.ntf
GE01p_dg = re.compile("GE01_\w+-M")

# GE01_111211P0011184144A222000100082M_000754776.ntf
GE01p = re.compile("GE01_\w+M0")

# IK01_2009121113234710000011610960_pan_6516S.ntf
IK01p = re.compile("IK01_\w+(blu|msi|bgrn)")

dRegExs = {
    WV02p:("WV02"),
    GE01p_dg:("GE01"),
    WV03p:("WV03"),
    QB02p:("QB02"),
    GE01p:("GE01"),
    IK01p:("IK01")
}

def get_panchromatic_name(sensor,mul_path):

    ####  check for pan version
    mul_dir, mul_name = os.path.split(mul_path)
    
    if sensor in ["WV02","WV03","QB02"]:
        pan_name = mul_name.replace("-M","-P")
    elif sensor == "GE01":
        if "_5V" in mul_name:
            
            pan_name_base = mul_path[:-24].replace("M0","P0")
            candidates = glob.glob(pan_name_base + "*")
            candidates2 = [f for f in candidates if f.endswith(('.ntf','.NTF','.tif','.TIF'))]
            if len(candidates2) == 0:
                pan_name = ''
            elif len(candidates2) == 1:
                pan_name = os.path.basename(candidates2[0])
            else: #raise error for now. TODO: iterate through candidates for greatest overlap
                pan_name = ''
                logger.error('{} panchromatic images match the multispectral image name {}'.format(len(candidates2),mul_name))
        else:
            pan_name = mul_name.replace("-M","-P")
    elif sensor == "IK01":
        pan_name = mul_name.replace("blu","pan")
        pan_name = mul_name.replace("msi","pan")
        pan_name = mul_name.replace("bgrn","pan")

    return pan_name



def main():

    #### Set Up Arguments
    parent_parser, pos_arg_keys = ortho_functions.buildParentArgumentParser()
    parser = argparse.ArgumentParser(
        parents=[parent_parser],
        description="Run/Submit batch pansharpening in parallel"
    )

    parser.add_argument("-l", help="PBS resources requested (mimicks qsub syntax)")
    parser.add_argument("--qsubscript",
                    help="qsub script to use in cluster job submission (default is qsub_pansharpen.sh in script root folder)")
    parser.add_argument("--pbs", action='store_true', default=False,
                    help="submit tasks to PBS")
    parser.add_argument("--parallel-processes", type=int, default=1,
                    help="number of parallel processes to spawn (default 1)")
    parser.add_argument("--dryrun", action="store_true", default=False,
                    help="print actions without executing")

    #### Parse Arguments
    args = parser.parse_args()
    scriptpath = os.path.abspath(sys.argv[0])
    src = os.path.abspath(args.src)
    dstdir = os.path.abspath(args.dst)

    #### Validate Required Arguments
    if os.path.isdir(src):
        srctype = 'dir'
    elif os.path.isfile(src) and os.path.splitext(src)[1].lower() == '.txt':
        srctype = 'textfile'
    elif os.path.isfile(src) and os.path.splitext(src)[1].lower() in ortho_functions.exts:
        srctype = 'image'
    elif os.path.isfile(src.replace('msi','blu')) and os.path.splitext(src)[1].lower() in ortho_functions.exts:
        srctype = 'image'
    else:
        parser.error("Error arg1 is not a recognized file path or file type: %s" %(src))

    if not os.path.isdir(dstdir):
        parser.error("Error arg2 is not a valid file path: %s" %(dstdir))

    ## Verify qsubscript
    if args.qsubscript is None:
        qsubpath = os.path.join(os.path.dirname(scriptpath),'qsub_pansharpen.sh')
    else:
        qsubpath = os.path.abspath(args.qsubscript)
    if not os.path.isfile(qsubpath):
        parser.error("qsub script path is not valid: %s" %qsubpath)

    ## Verify processing options do not conflict
    if args.pbs and args.parallel_processes > 1:
        parser.error("Options --pbs and --parallel-processes > 1 are mutually exclusive")

    #### Verify EPSG
    try:
        spatial_ref = utils.SpatialRef(args.epsg)
    except RuntimeError, e:
        parser.error(e)
        
    ## Check GDAL version (2.1.0 minimum)
    gdal_version = gdal.VersionInfo()
    try:
        if int(gdal_version) < 2010000:
            parser.error("gdal_pansharpen requires GDAL version 2.1.0 or higher")
    except ValueError, e:
        parser.error("Cannot parse GDAL version: {}".format(gdal_version))

    #### Set up console logging handler
    lso = logging.StreamHandler()
    lso.setLevel(logging.DEBUG)
    formatter = logging.Formatter('%(asctime)s %(levelname)s- %(message)s','%m-%d-%Y %H:%M:%S')
    lso.setFormatter(formatter)
    logger.addHandler(lso)
    
    #### Get args ready to pass to task handler
    arg_keys_to_remove = ('l', 'qsubscript', 'dryrun', 'pbs', 'parallel_processes')
    arg_str_base = utils.convert_optional_args_to_string(args, pos_arg_keys, arg_keys_to_remove)
    
    ## Identify source images
    if srctype == 'dir':
        image_list1 = utils.find_images(src, False, ortho_functions.exts)
    elif srctype == 'textfile':
        image_list1 = utils.find_images(src, True, ortho_functions.exts)
    else:
        image_list1 = [src]

    image_list = []
    for srcfp in image_list1:
        #print  srcfp
        srcdir, srcfn = os.path.split(srcfp)

        ####  Identify name pattern
        sensor = None
        for regex in dRegExs:
            match = regex.match(srcfn)
            if match is not None:
                sensor = dRegExs[regex]
                break
        if sensor:
            print "Image: {}, Sensor: {}".format(srcfn,sensor)    
            pan_name = get_panchromatic_name(sensor,srcfp)
            pan_srcfp = os.path.join(srcdir,pan_name)
            if not os.path.isfile(pan_srcfp):
                logger.error("Corresponding panchromatic image not found: %s" %(srcfp))
            else:
                image_list.append(srcfp)
                
    logger.info('Number of src images: {}'.format(len(image_list)))
    
    ## Build task queue
    i = 0
    task_queue = []
    for srcfp in image_list:
        srcdir, srcfn = os.path.split(srcfp)
        
        bittype = utils.get_bit_depth(args.outtype)
        pansh_dstfp = os.path.join(dstdir,"{}_{}{}{}_pansh.tif".format(os.path.splitext(srcfn)[0],bittype,args.stretch,args.epsg))
        
        if not os.path.isfile(pansh_dstfp):
            i+=1
            task = utils.Task(
                srcfn,
                'Pansh{:04g}'.format(i),
                'python',
                '{} {} {} {}'.format(scriptpath, arg_str_base, srcfp, dstdir),
                exec_pansharpen,
                [srcfp, pansh_dstfp, args]
            )
            task_queue.append(task)
            
    logger.info('Number of incomplete tasks: {}'.format(i)) 

    ## Run tasks
    if len(task_queue) > 0:
        logger.info("Submitting Tasks")
        if args.pbs:
            if args.l:
                task_handler = utils.PBSTaskHandler(qsubpath, "-l {}".format(args.l))
            else:
                task_handler = utils.PBSTaskHandler(qsubpath)
            if not args.dryrun:
                task_handler.run_tasks(task_queue)
            
        elif args.parallel_processes > 1:
            task_handler = utils.ParallelTaskHandler(args.parallel_processes)
            logger.info("Number of child processes to spawn: {0}".format(task_handler.num_processes))
            if not args.dryrun:
                task_handler.run_tasks(task_queue)
    
        else:
            results = {}
            for task in task_queue:
                           
                src, dstfp, task_arg_obj = task.method_arg_list
                
                #### Set up processing log handler
                logfile = os.path.splitext(dstfp)[0]+".log"
                lfh = logging.FileHandler(logfile)
                lfh.setLevel(logging.DEBUG)
                formatter = logging.Formatter('%(asctime)s %(levelname)s- %(message)s','%m-%d-%Y %H:%M:%S')
                lfh.setFormatter(formatter)
                logger.addHandler(lfh)
                
                if not args.dryrun:
                    results[task.name] = task.method(src, dstfp, task_arg_obj)
            
            #### Print Images with Errors    
            for k,v in results.iteritems():
                if v != 0:
                    logger.warning("Failed Image: {}".format(k))
        
        logger.info("Done")
        
    else:
        logger.info("No images found to process")


def exec_pansharpen(mul_srcfp, pansh_dstfp, args):

    srcdir,srcfn = os.path.split(mul_srcfp)
    dstdir = os.path.dirname(pansh_dstfp)

    #### Get working dir
    if args.wd is not None:
        wd = args.wd
    else:
        wd = dstdir
    if not os.path.isdir(wd):
        try:
            os.makedirs(wd)
        except OSError:
            pass
    logger.info("Working Dir: %s" %wd)

    ####  Identify name pattern
    sensor = None
    for regex in dRegExs:
        match = regex.match(srcfn)
        if match is not None:
            sensor = dRegExs[regex]
            break

    pan_name = get_panchromatic_name(sensor,mul_srcfp)
    pan_srcfp = os.path.join(srcdir,pan_name)
    print "Multispectral image: %s" %mul_srcfp
    print "Panchromatic image: %s" %pan_srcfp

    if args.dem is not None:
        dem_arg = '-d "%s" ' %args.dem
    else:
        dem_arg = ""

    bittype = utils.get_bit_depth(args.outtype)
    pan_basename = os.path.splitext(pan_name)[0]
    mul_basename = os.path.splitext(srcfn)[0]
    pan_local_dstfp = os.path.join(wd,"{}_{}{}{}.tif".format(pan_basename,bittype,args.stretch,args.epsg))
    mul_local_dstfp = os.path.join(wd,"{}_{}{}{}.tif".format(mul_basename,bittype,args.stretch,args.epsg))
    pan_dstfp = os.path.join(dstdir,"{}_{}{}{}.tif".format(pan_basename,bittype,args.stretch,args.epsg))
    mul_dstfp = os.path.join(dstdir,"{}_{}{}{}.tif".format(mul_basename,bittype,args.stretch,args.epsg))
    pansh_tempfp = os.path.join(wd,"{}_{}{}{}_pansh_temp.tif".format(mul_basename,bittype,args.stretch,args.epsg))
    pansh_local_dstfp = os.path.join(wd,"{}_{}{}{}_pansh.tif".format(mul_basename,bittype,args.stretch,args.epsg))
    pansh_xmlfp = os.path.join(dstdir,"{}_{}{}{}_pansh.xml".format(mul_basename,bittype,args.stretch,args.epsg))
    mul_xmlfp = os.path.join(dstdir,"{}_{}{}{}.xml".format(mul_basename,bittype,args.stretch,args.epsg))
    
    if not os.path.isdir(wd):
        os.makedirs(wd)

    ####  Ortho pan
    logger.info("Orthorectifying panchromatic image")
    if not os.path.isfile(pan_dstfp) and not os.path.isfile(pan_local_dstfp):
        rc = ortho_functions.process_image(pan_srcfp,pan_dstfp,args)

    if not os.path.isfile(pan_local_dstfp) and os.path.isfile(pan_dstfp):
        shutil.copy2(pan_dstfp,pan_local_dstfp)

    logger.info("Orthorectifying multispectral image")
    ####  Ortho multi
    if not os.path.isfile(mul_dstfp) and not os.path.isfile(mul_local_dstfp):
        ## If resolution is specified in the command line, assume it's intended for the pansharpened image
        ##    and multiply the multi by 4
        if args.resolution:
            args.resolution = args.resolution * 4.0
        rc = ortho_functions.process_image(mul_srcfp,mul_dstfp,args)

    if not os.path.isfile(mul_local_dstfp) and os.path.isfile(mul_dstfp):
        shutil.copy2(mul_dstfp,mul_local_dstfp)

    ####  Pansharpen
    logger.info("Pansharpening multispectral image")
    if os.path.isfile(pan_local_dstfp) and os.path.isfile(mul_local_dstfp):
        if not os.path.isfile(pansh_local_dstfp):
            cmd = 'gdal_pansharpen.py -co BIGTIFF=IF_SAFER -co COMPRESS=LZW -co TILED=YES "{}" "{}" "{}"'.format(pan_local_dstfp, mul_local_dstfp, pansh_local_dstfp)
            utils.exec_cmd(cmd)
    else:
        print "Pan or Multi warped image does not exist\n\t%s\n\t%s" %(pan_local_dstfp,mul_local_dstfp)

    #### Make pyramids
    if os.path.isfile(pansh_local_dstfp):
       cmd = 'gdaladdo "%s" 2 4 8 16' %(pansh_local_dstfp)
       utils.exec_cmd(cmd)
       
    ## Copy warped multispectral xml to pansharpened output
    shutil.copy2(mul_xmlfp,pansh_xmlfp)

    #### Copy pansharpened output
    if wd <> dstdir:
        for local_path, dst_path in [
            (pansh_local_dstfp,pansh_dstfp),
            (pan_local_dstfp,pan_dstfp),
            (mul_local_dstfp,mul_dstfp)
        ]:
            if os.path.isfile(local_path) and not os.path.isfile(dst_path):
                shutil.copy2(local_path,dst_path)

    #### Delete Temp Files
    wd_files = [
        pansh_local_dstfp,
        pan_local_dstfp,
        mul_local_dstfp
    ]

    if not args.save_temps:
        if wd <> dstdir:
            for f in wd_files:
                try:
                    os.remove(f)
                except Exception, e:
                    logger.warning('Could not remove %s: %s' %(os.path.basename(f),e))


if __name__ == '__main__':
    main()
