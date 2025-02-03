#! /usr/bin/env python3

import argparse
import os
import xml.etree.ElementTree as ET

def deal_yymm(yymm):
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
        print('rm ~hep, %d files' % (file_ct_4 - file_ct_3))

        os.system('rm %s' % tar_file)

    total_size = 0
    for dirpath, dirnames, filenames in os.walk(output_dir):
        for f in filenames:
            if f.endswith('.tar'):
                continue
            fp = os.path.join(dirpath, f)
            total_size += os.path.getsize(fp) / 1024**3
    print('done %s, total size: %.2f GB' % (yymm, total_size))

    os.system('tar -cf %s.tar %s/%s' % (yymm, output_dir, yymm))

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--yymm', type=str, default='0406')
    args = parser.parse_args()
    deal_yymm(args.yymm)
