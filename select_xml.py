#! /usr/bin/env python3

"""
load and wash and save arXiv_src_manifest.xml
"""

import argparse
import xml.etree.ElementTree as ET

def parse_file_element(file_elem):
    """将file元素解析为字典"""
    file_dict = {}
    for child in file_elem:
        file_dict[child.tag] = child.text
    return file_dict

def yymm_order(yymm):
    if yymm[0] == '9':
        return int(yymm) + 190000
    else:
        return int(yymm) + 200000

def stat_yymm():
    tree = ET.parse('arXiv_src_manifest.xml')
    root = tree.getroot()

    yymms = []
    for file_elem in root:
        if file_elem.tag != "file":
            continue

        file_dict = parse_file_element(file_elem)
        if len(yymms)==0 or yymms[-1]['yymm'] != file_dict['yymm']:
            yymms.append({
                'yymm': file_dict['yymm'],
                'file_count': 0
            })
        yymms[-1]['file_count'] += 1

    yymms.sort(key=lambda x: yymm_order(x['yymm']))
    print(yymms)

def select_yymm(yymm_perfix):
    tree = ET.parse('arXiv_src_manifest.xml')
    root = tree.getroot()

    yymms = []
    new_root = ET.Element(root.tag)
    for file_elem in root:
        if file_elem.tag != "file":
            continue

        file_dict = parse_file_element(file_elem)

        if file_dict['yymm'].startswith(yymm_perfix):
            new_root.append(file_elem)

    print('selected %d files out based on %s' % (len(new_root), yymm_perfix))
    save_path = 'arXiv_src_%s.xml' % yymm_perfix
    new_tree = ET.ElementTree(new_root)
    new_tree.write(save_path, encoding='utf-8', xml_declaration=True)
    print('saved to %s' % save_path)

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--yymm', type=str, default='0406')
    parser.add_argument('--stat', action='store_true')
    args = parser.parse_args()
    if args.stat:
        stat_yymm()
    else:
        select_yymm(args.yymm)
