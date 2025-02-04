#! /usr/bin/env python3

import argparse
import os
import xml.etree.ElementTree as ET

def deal_yymm(yymm):
    if os.path.exists("%s.tar" % yymm):
        print("%s.tar already exists" % yymm)
        return

    os.system('./select_xml.py --yymm %s' % yymm)

    manifest_file = 'arXiv_src_%s.xml' % yymm
    output_dir = 'arXiv_src_%s' % yymm
    os.makedirs(output_dir, exist_ok=True)
    os.system('python download.py --manifest_file %s --mode src --output_dir %s' % (manifest_file, output_dir))

    tree = ET.parse(manifest_file)
    xml_ct = sum(1 for i in tree.getroot() if i.tag == 'file')
    print('xml files: %d' % xml_ct)
    tar_ct = sum(1 for f in os.listdir(output_dir) if f.endswith('.tar'))
    print('tar files: %d' % tar_ct)
    assert xml_ct == tar_ct, 'xml files and tar files are not equal %d != %d' % (xml_ct, tar_ct)

    for tar_file in os.listdir(output_dir):
        if not tar_file.endswith('.tar'):
            continue

        yymm_this = tar_file.split('_')[2]
        def ct_file():
            return sum([len(files) for _, _, files in os.walk(os.path.join(output_dir, yymm_this))])
        file_ct_1 = ct_file()

        os.system('tar -xf %s/%s -C %s' % (output_dir, tar_file, output_dir))
        file_ct_2 = ct_file()
        print('extract %s, %d files' % (tar_file, file_ct_2 - file_ct_1))

        os.system('rm %s/%s/*.pdf' % (output_dir, yymm))
        file_ct_3 = ct_file()
        print('rm pdf, %d files' % (file_ct_3 - file_ct_2))

        os.system('rm %s/%s/[!h][!e][!p]*' % (output_dir, yymm))
        file_ct_4 = ct_file()
        print('rm ~hep, %d files, %d files left' % (file_ct_4 - file_ct_3, file_ct_4))

        os.system('rm %s/%s' % (output_dir, tar_file))

    total_size = 0
    for dirpath, dirnames, filenames in os.walk(output_dir):
        for f in filenames:
            if f.endswith('.tar'):
                continue
            fp = os.path.join(dirpath, f)
            total_size += os.path.getsize(fp) / 1024**3
    print('done %s, total size: %.2f GB' % (yymm, total_size))

    os.system('tar -cf %s.tar %s/%s' % (yymm, output_dir, yymm))

def clean_tar():
    for f in os.listdir():
        if not f.endswith('.tar'):
            continue

        size = os.path.getsize(f)/1024**2 # MB
        if size < 10:
            continue

        yymm = f.split('.')[0]
        if not os.path.exists("arXiv_src_%s" % yymm):
            continue

        # for f2 in os.listdir("arXiv_src_%s" % yymm):
        #     if not f2.endswith('.tar'):
        #         continue
        #     f2 = os.path.join("arXiv_src_%s" % yymm, f2)
        #     input("rm %s?" % f2)
        #     os.system('rm %s' % f2)
        #     print("rm %s" % f2)

        # input("rm arXiv_src_%s?" % yymm)
        os.system("rm -r arXiv_src_%s" % yymm)
        print("rm arXiv_src_%s" % yymm)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--yymm', type=str, default='0406')
    parser.add_argument('--clean', action='store_true')
    parser.add_argument('--all', action='store_true')
    args = parser.parse_args()
    if args.clean:
        clean_tar()
    elif args.all:
        # for yy in range(91, 100):
        for yy in range(0,7+1):
            for mm in range(1, 13):
                # if yy == 91 and mm < 7:
                #     continue
                if yy == 7 and mm == 3:
                    print("reached 0703, break")
                    break

                yymm = f"{yy:02d}{mm:02d}"
                # input("deal %s?" % yymm)
                deal_yymm(yymm)
    else:
        deal_yymm(args.yymm)
