#! /usr/bin/env python3

import os
import matplotlib.pyplot as plt
from scipy.interpolate import interp1d

def yymm2int(yymm):
    year  = int(yymm[:2])
    month = int(yymm[2:])
    epoch = 91*12 + 7

    if yymm.startswith('9'):
        return year * 12 + month - epoch
    else:
        return (year + 100) * 12 + month - epoch

def hep_trends():

    datas = []
    for f in os.listdir('./tars0703'):
        if not f.endswith('.tar'):
            continue
        year_str = f.split('.')[0]
        f = os.path.join('./tars0703', f)
        size = os.path.getsize(f)/1024**3
        datas.append((yymm2int(year_str), size, year_str))

    datas.sort(key=lambda x: x[0])

    print(datas)

    x = [i[0] for i in datas]
    y = [i[1] for i in datas]
    labels = [i[2] for i in datas]

    # how to get a interpolation of x and y using scipy?

    f = interp1d(x, y, kind='linear')
    est_tot_size = 0
    for i in range(min(x), max(x) + 1):
        est_tot_size += f(i)
    print("estimated total size: %.2f GB" % (est_tot_size))

    plt.plot(x, y)
    x_labels = []
    s_labels = []
    for i, x in enumerate(x):
        if len(x_labels) == 0 or x-x_labels[-1] >= 12:
            x_labels.append(x)
            s_labels.append(labels[i])
    plt.xticks(x_labels, s_labels, rotation=90)
    plt.xlabel('Year')
    plt.ylabel('Size (GB)')
    plt.title('HEP total Size Trends')
    plt.savefig('submission_size.png')

if __name__ == '__main__':
    # print(yymm2int('9107'))
    hep_trends()
