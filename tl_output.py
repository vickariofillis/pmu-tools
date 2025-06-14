# Copyright (c) 2012-2020, Intel Corporation
# Author: Andi Kleen
#
# This program is free software; you can redistribute it and/or modify it
# under the terms and conditions of the GNU General Public License,
# version 2, as published by the Free Software Foundation.
#
# This program is distributed in the hope it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or
# FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public License for
# more details.
#
# Output toplev results in various formats

from __future__ import print_function
import locale
import csv
import re
import sys
import json
import os
from math import isnan
from collections import defaultdict, Counter, OrderedDict
from tl_uval import UVal, combine_uval
from tl_io import flex_open_w
if sys.version_info.major == 3:
    import typing # noqa
    from typing import DefaultDict, Dict, Any # noqa

def output_name(name, typ):
    if typ:
        if "." in name:
            name = re.sub(r'(.*)\.', r'\1-%s.' % typ, name)
        else:
            name += "-" + typ
    return name

def open_logfile(name, typ):
    if name is None or name == "":
        return sys.stderr
    if 'write' in name.__class__.__dict__:
        return name
    name = output_name(name, typ)
    try:
        return flex_open_w(name)
    except IOError:
        sys.exit("Cannot open logfile %s" % name)

def open_all_logfiles(args, logfile):
    if args.split_output and args.per_thread + args.per_core + args.per_socket + args.global_ > 0:
        logfiles = dict()
        if args.per_thread:
            logfiles['thread'] = open_logfile(logfile, "thread")
        if args.per_core:
            logfiles['core'] = open_logfile(logfile, "core")
        if args.per_socket:
            logfiles['socket'] = open_logfile(logfile, "socket")
        if args.global_:
            logfiles['global'] = open_logfile(logfile, "global")
        return logfiles, None
    else:
        return None, open_logfile(logfile, None)

BUFS = 1024*1024

def catrmfile(infn, outf, keep):
    with open(infn, "r") as inf:
        while True:
            buf = inf.read(BUFS)
            if len(buf) == 0:
                break
            outf.write(buf)
    outf.flush()
    if not keep:
        os.remove(infn)

def catrmoutput(infn, logf, logfiles, keep):
    if logfiles:
        for j in logfiles.keys():
            catrmfile(output_name(infn, j), logfiles[j], keep)
    else:
        catrmfile(infn, logf, keep)

class Output(object):
    """Abstract base class for Output classes."""
    def __init__(self, logfile, version, cpu, args):
        self.logfiles, self.logf = open_all_logfiles(args, args.output)
        self.printed_descs = set()
        self.hdrlen = 30
        self.version = version
        self.unitlen = 12
        self.belowlen = 0
        self.version = "%s on %s [%s%s]" % (version, cpu.name, cpu.true_name,
                "/" + cpu.pmu_name if cpu.pmu_name else "")
        self.curname = ""
        self.curname_nologf = ""
        self.printedversion = set()
        self.no_header = args.no_csv_header
        self.no_footer = args.no_csv_footer
        self.abbrev = args.abbrev
        self.valcsv = None
        self.last_prefix = ""
        self.args = args

    def flushfiles(self):
        if self.logfiles:
            for j in self.logfiles.values():
                j.flush()
        self.logf.flush()

    # pass all possible hdrs in advance to compute suitable padding
    def set_hdr(self, hdr, area):
        if area:
            hdr = "%-16s %s" % (area, hdr)
        self.hdrlen = max(len(hdr) + 1, self.hdrlen)

    def set_below(self, below):
        if below:
            self.belowlen = 1

    def set_unit(self, unit):
        self.unitlen = max(len(unit), self.unitlen)

    def set_cpus(self, cpus):
        pass

    def item(self, area, name, uval, timestamp, unit, desc, title, sample, bn, below, idle, runs=None):
        assert isinstance(uval, UVal)
        # --
        if desc in self.printed_descs:
            desc = ""
        else:
            self.printed_descs.add(desc)
        if not area:
            area = ""
        self.show(timestamp, title, area, name, uval, unit, desc, sample, bn, below, idle, runs)

    def ratio(self, area, name, uval, timestamp, unit, desc, title, sample, bn, below, idle, runs=None):
        uval.is_ratio = True
        self.item(area, name, uval, timestamp, unit, desc, title, sample, bn, below, idle, runs)

    def metric(self, area, name, uval, timestamp, desc, title, unit, idle, runs=None):
        self.item(area, name, uval, timestamp, unit, desc, title, None, "", "", idle, runs)

    def flush(self):
        pass

    def remark(self, m):
        if not self.logfiles:
            self.logf.write('\n%s:\n' % m)

    def reset(self, name):
        if self.logfiles:
            self.logf = self.logfiles[name]
            self.curname = name
        self.curname_nologf = name

    def show(self, timestamp, title, area, hdr, val, unit, desc, sample, bn, below, idle, runs=None):
        pass

    def print_version(self):
        if self.no_header or self.args.no_version:
            return
        if self.curname not in self.printedversion:
            if self.logfiles:
                self.logfiles[self.curname].write("# " + self.version + "\n")
            else:
                self.logf.write("# " + self.version + "\n")
            self.printedversion.add(self.curname)

    print_header = print_version

    def print_footer(self):
        pass

    def print_footer_all(self):
        if self.no_footer or self.args.no_version:
            return
        if self.logfiles:
            for f in self.logfiles.values():
                f.write("# %s\n" % self.version)
        else:
            self.logf.write("# " + self.version + "\n")

def fmt_below(below):
    if below:
        return "<"
    return ""

def short_hdr(hdr, last):
    n = os.path.commonprefix((hdr, last))
    if "." not in n:
        return hdr
    n = n[:n.rfind(".")]
    return "..." + hdr[len(n)+1:]

class OutputHuman(Output):
    """Generate human readable single-column output."""
    def __init__(self, logfile, args, version, cpu):
        Output.__init__(self, logfile, version, cpu, args)
        try:
            locale.setlocale(locale.LC_ALL, '')
        except locale.Error:
            pass
        self.args = args
        self.titlelen = 7

    def set_cpus(self, cpus):
        if len(cpus) > 0:
            self.titlelen = max(map(len, cpus)) + 1

    def print_desc(self, desc, sample):
        if self.args.no_desc:
            return
        if desc:
            print("\t" + desc, file=self.logf)
            if sample:
                print("\t" + "Sampling events: ", sample, file=self.logf)

    def print_timestamp(self, timestamp):
        if timestamp:
            if isnan(timestamp):
                self.logf.write("%-11s " % "SUMMARY")
            else:
                self.logf.write("%6.9f " % timestamp)

    def print_line_header(self, area, ohdr):
        if "Info" in area or not self.abbrev:
            hdr = ohdr
        else:
            hdr = short_hdr(ohdr, self.last_prefix)
            self.last_prefix = ohdr
        if area:
            hdr = "%-16s %s" % (area, hdr)
        self.logf.write("%-*s " % (self.hdrlen, hdr))

    # timestamp Timestamp in interval mode
    # title     CPU
    # area      FE/BE ...
    # hdr       Node Name
    # val       Formatted measured value
    # unit      unit
    # desc      Object description
    # sample    Sample Objects (string)
    # vs        Statistics object
    # bn        marker for bottleneck
    # below     True if below
    # idle      Idle marker (ignored for Human)
    # Example:
    # C0    BE      Backend_Bound:                                62.00 %
    def show(self, timestamp, title, area, hdr, val, unit, desc, sample, bn, below, idle, runs=None):
        self.print_header()
        self.print_timestamp(timestamp)
        write = self.logf.write
        if title:
            write("%-*s" % (self.titlelen, title))
        self.print_line_header(area, hdr)
        vals = "{:<{unitlen}} {:>20} {:<{belowlen}}".format(
                    ("  " if unit and unit[0] != "%" else "") + unit,
                    val.format_value(unit),
                    fmt_below(below),
                    unitlen=self.unitlen + 2,
                    belowlen=self.belowlen)
        if not self.args.no_mux and val.multiplex != 100.0:
            vals += " " + val.format_mux()
        if val.stddev:
            vals += " +- {:>8}".format(val.format_uncertainty())
        if bn:
            vals += bn
        write(vals + "\n")
        self.print_desc(desc, sample)

    def metric(self, area, name, uval, timestamp, desc, title, unit, idle, runs=None):
        self.item(area, name, uval, timestamp, unit, desc, title, None, "", "", False, runs)

def convert_ts(ts):
    if isnan(ts):
        return "SUMMARY"
    return ts

class OutputColumns(OutputHuman):
    """Human-readable output data in per-cpu columns."""
    def __init__(self, logfile, args, version, cpu):
        OutputHuman.__init__(self, logfile, args, version, cpu)
        self.nodes = {}
        self.timestamp = None
        self.cpunames = set()
        self.printed_header = False

    def set_cpus(self, cpus):
        self.cpunames = cpus

    def show(self, timestamp, title, area, hdr, val, unit, desc, sample, bn, below, idle, runs=None):
        if self.args.single_thread:
            OutputHuman.show(self, timestamp, title, area, hdr, val, unit, desc, sample, bn, below, idle)
            return
        self.print_header()
        self.timestamp = timestamp
        key = (area, hdr)
        if key not in self.nodes:
            self.nodes[key] = {}
        assert title not in self.nodes[key]
        self.nodes[key][title] = (val, unit, desc, sample, bn, below, idle)

    def flush(self):
        VALCOL_LEN = 16
        write = self.logf.write

        cpunames = sorted(self.cpunames)

        if not self.printed_header:
            if self.timestamp:
                write("%9s" % "")
            self.print_line_header("", "")
            for j in cpunames:
                write("%*s  " % (VALCOL_LEN, j))
            write("\n")
            self.printed_header = True

        for key in sorted(sorted(self.nodes.keys(), key=lambda x: x[1]), key=lambda x: x[0] == ""):
            node = self.nodes[key]
            desc = None
            sample = None
            unit = None
            if self.timestamp:
                self.print_timestamp(self.timestamp)

            self.print_line_header(key[0], key[1])
            vlist = []
            for cpuname in cpunames:
                if cpuname in node:
                    cpu = node[cpuname]
                    uval, unit, desc, sample, bn, below, idle = cpu
                    v = uval.format_value(unit)
                    vlist.append(uval)
                    write("%*s%s " % (VALCOL_LEN, v, "?" if below else "*" if bn else " "))
                else:
                    write("%*s  " % (VALCOL_LEN, ""))
            if unit:
                # XXX should move this to be per entry?
                cval = combine_uval(vlist)
                vs = (" +- " + cval.format_uncertainty() + " " + cval.format_mux()) if cval.stddev else ""
                write(" %-*s%s" % (self.unitlen, ("  " if unit[0] != "%" else "") + unit, vs))
            write("\n")
            self.print_desc(desc, sample)
        self.nodes = {}

    def reset(self, name):
        Output.reset(self, name)
        self.printed_header = False

class OutputColumnsCSV(OutputColumns):
    """Columns output in CSV mode."""

    def __init__(self, logfile, sep, args, version, cpu):
        OutputColumns.__init__(self, logfile, args, version, cpu)
        self.writer = {}
        if self.logfiles:
            for n, f in self.logfiles.items():
                self.writer[n] = csv.writer(f, delimiter=sep, lineterminator='\n')
        else:
            self.writer[''] = csv.writer(self.logf, delimiter=sep, lineterminator='\n')
        self.printed_header = False

    # XXX implement bn and idle
    def show(self, timestamp, title, area, hdr, val, unit, desc, sample, bn, below, idle, runs=None):
        self.print_header()
        self.timestamp = timestamp
        key = (area, hdr)
        if key not in self.nodes:
            self.nodes[key] = {}
        assert title not in self.nodes[key]
        self.nodes[key][title] = (val, unit + " " + fmt_below(below), desc, sample, runs)

    def flush(self):
        cpunames = sorted(self.cpunames)
        if not self.printed_header and not self.no_header:
            ts = ["Timestamp"] if self.timestamp else []
            header = ts + ["Area", "Node"] + cpunames + ["Description", "Sample", "Stddev", "Multiplex", "Run"]
            self.writer[self.curname].writerow([x for x in header])
            self.printed_header = True
        for key in sorted(sorted(self.nodes.keys(), key=lambda x: x[1]), key=lambda x: x[0] == ""):
            node = self.nodes[key]
            ts = [convert_ts(self.timestamp)] if self.timestamp else []
            l = ts + [key[0], key[1]]
            vlist = []
            ol = {}
            desc, sample = "", ""
            for cpuname in cpunames:
                if cpuname in node:
                    cpu = node[cpuname]
                    if cpu[2]:
                        desc = cpu[2]
                        desc = re.sub(r"\s+", " ", desc)
                    if cpu[3]:
                        sample = cpu[3]
                    # ignore unit for now
                    vlist.append(cpu[0])
                    ol[cpuname] = float(cpu[0].value) if cpu[0].value else ""
                else:
                    vlist.append(UVal("",0))
            l += [ol[x] if x in ol else "" for x in cpunames]
            l.append(desc)
            l.append(sample)
            vs = combine_uval(vlist)
            if vs:
                l += (vs.format_uncertainty().strip(), vs.format_mux().strip())
            else:
                l += ["", ""]
            run_list = None
            for cpu in node.values():
                if len(cpu) > 4 and cpu[4]:
                    run_list = cpu[4]
                    break
            run_str = "" if not run_list else "|".join(str(r) for r in sorted(run_list))
            l.append(run_str)
            self.writer[self.curname].writerow(l)
        self.nodes = {}

    print_footer = Output.print_footer_all

class OutputCSV(Output):
    """Output data in CSV format."""
    def __init__(self, logfile, sep, args, version, cpu):
        Output.__init__(self, logfile, version, cpu, args)
        self.writer = {}
        if self.logfiles:
            for n, f in self.logfiles.items():
                self.writer[n] = csv.writer(f, delimiter=sep, lineterminator='\n')
        else:
            self.writer[''] = csv.writer(self.logf, delimiter=sep, lineterminator='\n')
        self.args = args
        self.printed_headers = set()

    def print_header_csv(self, timestamp, title):
        if self.no_header:
            return
        if self.curname_nologf not in self.printed_headers:
            l = []
            if timestamp:
                l.append("Timestamp")
            if title:
                l.append("CPUs")
            self.writer[self.curname].writerow(l +
                ['Area', 'Value', 'Unit', 'Description',
                 'Sample', 'Stddev', 'Multiplex', 'Bottleneck', 'Idle', 'Run'])
            self.printed_headers.add(self.curname_nologf)

    def show(self, timestamp, title, area, hdr, val, unit, desc, sample, bn, below, idle, runs=None):
        self.print_header_csv(timestamp, title)
        if self.args.no_desc:
            desc = ""
        desc = re.sub(r"\s+", " ", desc)
        l = []
        if timestamp:
            l.append(convert_ts(timestamp))
        if title:
            l.append("CPU" + title if re.match(r'[0-9]+', title) else title)
        stddev = val.format_uncertainty().strip()
        multiplex = val.multiplex if not isnan(val.multiplex) else ""
        run_str = "" if not runs else "|".join(str(r) for r in sorted(runs))
        self.writer[self.curname].writerow(l + [hdr, val.format_value_raw().strip(),
                                  (unit + " " + fmt_below(below)).strip(),
                                  desc, sample, stddev, multiplex, bn, "Y" if idle else "", run_str])

    print_footer = Output.print_footer_all

class OutputJSON(Output):
    """Output data in chrome / trace-viewer JSON format."""
    def __init__(self, logfile, sep, args, version, cpu):
        Output.__init__(self, logfile, version, cpu, args)
        self.nodes = defaultdict(dict) # type: DefaultDict[str, Dict[str, Any]]
        self.headers = OrderedDict()
        self.count = Counter() # type: typing.Counter[str]
        self.no_header = args.no_json_header
        self.no_footer = args.no_json_footer
        self.num = 0

    def print_footer_all(self):
        def write_all(s):
            if self.logfiles:
                for n in self.logfiles:
                    self.logfiles[n].write(s(n))
            else:
                self.logf.write(s(""))

        if self.no_footer:
            if self.num > 0:
                write_all(lambda x: ",\n")
        else:
            def start(name):
                n = ""
                if name not in self.count:
                    n += "[\n"
                return n + "\n]\n"
            write_all(start)

    print_footer = print_footer_all

    def show(self, timestamp, title, area, hdr, val, unit, desc, sample, bn, below, idle, runs=None):
        self.timestamp = timestamp
        self.nodes[title][hdr] = val
        self.headers[hdr] = True
        self.num += 1

    def flush(self):
        nodes = OrderedDict() # type: OrderedDict[str,Any]
        for hdr in self.headers:
            for title in sorted(self.nodes.keys()):
                if hdr not in self.nodes[title]:
                    continue
                nd = self.nodes[title]
                val = nd[hdr].value if isinstance(nd[hdr], UVal) else nd[hdr]

                if title:
                    title += " "
                if hdr in ("Frontend_Bound", "Backend_Bound", "BadSpeculation", "Retiring"): # XXX
                    key = title + "Level1"
                    if key not in nodes:
                        nodes[key] = {}
                    nodes[title + "Level1"][hdr] = val
                elif hdr.count(".") >= 1:
                    dot = hdr.rindex(".")
                    nodes[title + hdr[:dot]] = { hdr: round(val, 2) }
                else: # assume it's metric
                    nodes[title + hdr] = {hdr: val}

        for name in nodes.keys():
            if self.count[self.curname] == 0:
                if not self.no_header:
                    self.logf.write("[\n")
            else:
                self.logf.write(",\n")
            json.dump({"name": name,
                      "ph": "C",
                      "pid": 0,
                      "ts": self.timestamp / 1e6 if self.timestamp and not isnan(self.timestamp) else 0,
                      "args": nodes[name]}, self.logf)
            self.count[self.curname] += 1
        self.nodes = defaultdict(dict)
        self.headers = OrderedDict()

    def remark(self, m):
        pass
