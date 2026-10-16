#!/usr/bin/env python3
"""make_spike_table.py -- writes docs/sweep_spike_table.drawio from the numbers of sweep_spike.py (nothing typed by hand).

  python tools/make_spike_table.py --seg tools/examples/generated/entsoe_load_gr.seg \\
      --xml data/entsoe_actual_total_load_GR_2026-09-25_to_10-01.xml --out docs/sweep_spike_table.drawio

Left table : result per spike size (found, accuracy, TP, FP, FN, F1). Right table: false alarms of the calm part per location.
"""
import argparse
import html
import os
import sys
import xml.dom.minidom

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import sweep_spike as sw

HEAD = 'rounded=0;whiteSpace=wrap;html=1;fillColor=#1F3864;fontColor=#FFFFFF;fontStyle=1;strokeColor=#000000;fontSize=11;'


def comma(x):
    return '-' if x is None else ('%.2f' % x).replace('.', ',')


def build(values, hours, profile, level=0.5, positions=20):
    sizes = [0] + [i / 100 for i in range(1, 13)] + [0.15, 0.20, 0.30]
    rows, begins = sw.sweep(values, hours, profile, sizes, positions)
    locs = sw.per_location(values, hours, profile, positions)
    mn = sw.minimum_detectable([r for r in rows if r['size'] > 0], level)
    cells, n = [], [10]

    def cell(val, x, y, w, h, style):
        n[0] += 1
        cells.append(f'<mxCell id="c{n[0]}" parent="1" style="{style}" value="{html.escape(val, quote=True)}" vertex="1">'
                     f'<mxGeometry height="{h}" width="{w}" x="{x}" y="{y}" as="geometry"/></mxCell>')

    def table(x0, y0, cols, data, mark=None, total=None):
        x = x0
        for name, w in cols:
            cell(name, x, y0, w, 40, HEAD)
            x += w
        y = y0 + 40
        for i, vals in enumerate(data):
            fill = '#C6E0B4' if mark == i else ('#F2F2F2' if i % 2 else '#FFFFFF')
            x = x0
            for (name, w), v in zip(cols, vals):
                cell(v, x, y, w, 28, f'rounded=0;whiteSpace=wrap;html=1;fillColor={fill};strokeColor=#000000;fontSize=11;'
                     + ('fontStyle=1;' if mark == i else ''))
                x += w
            y += 28
        if total:
            x = x0
            for (name, w), v in zip(cols, total):
                cell(v, x, y, w, 28, 'rounded=0;whiteSpace=wrap;html=1;fillColor=#D9E2F3;fontStyle=1;strokeColor=#000000;fontSize=11;')
                x += w
            y += 28
        return y

    cell('Πείραμα spike (ENTSO-E): αριστερά, σε πόσες από τις 20 θέσεις βρέθηκε το spike (κανόνας p1)· δεξιά, ψευδείς συναγερμοί του τμήματος ηρεμίας ανά θέση',
         40, 10, 1000, 40, 'text;html=1;fontStyle=1;fontSize=14;align=left;verticalAlign=middle;whiteSpace=wrap;')
    cols1 = [('Μέγεθος spike', 150), ('Βρέθηκε (από 20)', 90), ('Accuracy (= Recall)', 110), ('TP', 55), ('FP', 60), ('FN', 55), ('F1', 70)]
    data1, mark = [], None
    for i, r in enumerate(rows):
        s = r['size']
        if mn is not None and abs(s - mn) < 1e-9:
            mark = i
        data1.append(['0% (χωρίς ένεση)' if s == 0 else f'{s * 100:.0f}%', f"{r['detected']}/{r['runs']}",
                      comma(r['accuracy']), str(r['TP']), str(r['FP']), str(r['FN']), comma(r['f1'])])
    y1 = table(40, 60, cols1, data1, mark)
    cols2 = [('Θέση (ξεκίνημα)', 100), ('Ώρα έναρξης (τοπική)', 110), ('p1', 70), ('p2', 70)]

    def hh(h):
        return f'{int(h):02d}:{int(round((h - int(h)) * 60)):02d}'
    data2 = [[str(r['begin']), hh(r['hour']), str(r['p1']), str(r['p2'])] for r in locs]
    t1, t2 = sum(r['p1'] for r in locs), sum(r['p2'] for r in locs)
    y2 = table(740, 60, cols2, data2, total=['Σύνολο', f'{len(locs) * (sw.CALM - 10)} δείγματα', str(t1), str(t2)])
    note = (f'Πράσινη γραμμή: ελάχιστη ανιχνεύσιμη ανωμαλία = {mn * 100:.0f}% (όριο accuracy {comma(level)}, επιλογή μας). '
            'Ανίχνευση = το p1 χτυπά στο δείγμα του spike. Κάθε θέση: 60 δείγματα ηρεμίας, 1 spike, 60 μετά, ψυχρή εκκίνηση του ανιχνευτή. '
            'TP, FP, FN: άθροισμα των 20 θέσεων· το FP μετράει συναγερμούς σε δείγματα χωρίς spike, πριν και μετά (2200 κρινόμενα δείγματα), άρα σχεδόν δεν εξαρτάται από το μέγεθος. '
            'Τα πρώτα 10 δείγματα κάθε θέσης (warm-up) δεν κρίνονται. F1 = 2·Precision·Recall / (Precision + Recall). '
            'Δεξιά: ψευδείς συναγερμοί πριν από το spike, 50 κρινόμενα δείγματα ανά θέση· το άθροισμα (p1 ' + str(t1) + ', p2 ' + str(t2) + ') είναι ανά 1000 δείγματα. '
            'Μέθοδος: Nixtla, 26/08/2025 (blog). Αριθμοί: tools/sweep_spike.py.')
    cell(note, 40, max(y1, y2) + 15, 1000, 110, 'text;html=1;fontSize=11;align=left;verticalAlign=top;whiteSpace=wrap;')
    doc = ('<mxfile host="app.diagrams.net"><diagram name="Πείραμα spike" id="spike1"><mxGraphModel dx="1432" dy="710" grid="1" gridSize="10" '
           'guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="1400" pageHeight="1000" math="0" shadow="0">'
           '<root><mxCell id="0"/><mxCell id="1" parent="0"/>' + ''.join(cells) + '</root></mxGraphModel></diagram></mxfile>')
    xml.dom.minidom.parseString(doc)          # must be well-formed XML
    return doc


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--seg', required=True)
    ap.add_argument('--xml', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--level', type=float, default=0.5)
    a = ap.parse_args()
    values, hours, profile = sw.load(a.xml, a.seg)
    open(a.out, 'w', encoding='utf-8').write(build(values, hours, profile, a.level))
    print('written', a.out)


if __name__ == '__main__':
    main()
