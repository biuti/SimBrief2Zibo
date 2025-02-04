"""
SimBrief2Zibo
X-Plane plugin

Copyright (c) 2025, Antonio Golfari
All rights reserved.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree. 
"""

from __future__ import annotations

import os
import json
import threading
import requests

from pathlib import Path
from urllib import parse
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta
from time import perf_counter

try:
    from XPPython3 import xp
except ImportError:
    print('xp module not found')


# Version
__VERSION__ = 'v1.7-beta.1'

# Plugin parameters required from XPPython3
plugin_name = 'SimBrief2Zibo'
plugin_sig = 'xppython3.simbrief2zibo'
plugin_desc = 'Fetches latest OFP Data from SimBrief and creates the file ZIBO B738 requires'

# Dref and Command parameters
plugin_command_origin = 'simbrief2zibo'

# Aircrafts
AIRCRAFTS = [
    ('Zibo', 'B737-800X'),
    ('LevelUp', 'LevelUp')
]

# Other parameters
DEFAULT_SCHEDULE = 15  # positive numbers are seconds, 0 disabled, negative numbers are cycles
DAYS = 2  # how recent a fp file has to be to be considered
DATIS = True # to avoid displaying DATIS widget waiting for a new website for Digital Atis info

# widget parameters
try:
    FONT = xp.Font_Proportional
    FONT_WIDTH, FONT_HEIGHT, _ = xp.getFontDimensions(FONT)
except NameError:
    FONT_WIDTH, FONT_HEIGHT = 10, 10

DETAILS_WIDTH = 240
ATIS_WIDTH = DETAILS_WIDTH * 2


def get_unsecure_url(url: str) -> str:
    parsed = parse.urlparse(url)
    parsed = parsed._replace(scheme=parsed.scheme.replace('https', 'http'))
    return parse.urlunparse(parsed)


def get_from_url(url: str) -> tuple[bool | requests.Response, str | int | None]:
    response = False
    error = None
    try:
        response = requests.get(url, verify=True)
    except requests.exceptions.SSLError as e:
        # change link to unsecure protocol to avoid SSL error in some weird systems
        print(f" *** connection to {url} had to run in unsecure mode: {e.args[0]}")
        try:
            link = get_unsecure_url(url)
            response = requests.get(link, verify=False, timeout=5)
        except Exception as e:
            print(f"*** SimBrief generic error: {e.args[0]}")
            error = e.args[0]
    except requests.exceptions.ConnectionError as e:
        print(f"*** SimBrief connection error: {e.args[0]}")
        error = e.args[0]
    finally:
        if isinstance(response, requests.Response) and response.status_code != 200:
            error = response.status_code
            print(f"*** SimBrief connection refused: {response.status_code} - {response.reason}")
    return response, error


class Async(threading.Thread):
    """Run an asynchronous task on a new thread

    Attributes:
        task (method): Worker method to be called
        die (threading.Event): Set the flag to end the tasks
        result (): return of the task method
    """

    def __init__(self, task, *args, **kwargs):

        self.pid = os.getpid()
        self.task = task
        self.cancel = threading.Event()
        self.kwargs = kwargs
        self.args = args
        self.elapsed = False
        self.result = False
        threading.Thread.__init__(self)

        self.pending = self.is_alive

    def run(self):
        start = perf_counter()
        try:
            self.result = self.task(*self.args, **self.kwargs)
        except Exception as e:
            self.result = e
        finally:
            self.elapsed = perf_counter() - start

    def stop(self):
        if self.is_alive():
            self.cancel.set()
            self.join(3)


class SimBrief(object):

    source = 'xml'
    uplink_filename = 'b738x'

    def __init__(self, pilot_id: str, path: Path, request_id: str | None) -> None:
        self.pilot_id = pilot_id
        self.path = path
        self.request_id = request_id
        self.ofp = None
        self.origin = None  # Departure ICAO
        self.destination = None  # Destination ICAO
        self.fp_link = None  # link to the SimBrief fms file
        self.coroute_filename = None  # fms/fmx filename
        self.error = None
        self.message = None
        self.fp_info = None
        self.result = False

    @property
    def xml_url(self) -> str:
        return f"https://www.simbrief.com/api/xml.fetcher.php?userid={self.pilot_id}"

    @property
    def json_url(self) -> str:
        return f"https://www.simbrief.com/api/xml.fetcher.php?userid={self.pilot_id}&json=1"

    @staticmethod
    def run(pilot_id: str, path: Path, request_id=None) -> dict:
        """
        return
        {'error', 'request_id', 'coroute_filename', 'message', 'fp_info'}
        """

        s = SimBrief(pilot_id, path, request_id)
        url = s.xml_url if s.source == 'xml' else s.json_url
        response = s.query(url)
        if response and not s.error:
            s.process(response)
        result = {
            'error': s.error,
            'request_id': s.request_id,
            'message': s.message,
            'fp_info': s.fp_info
        }
        return result

    def query(self, url: str) -> str | None:
        response, error = get_from_url(url)
        if error:
            if error == 400:
                # probably wrong pilotID
                self.message = "Error: is your pilotID correct?"
            else:
                self.message = "Error trying to connect to SimBrief"
            self.error = error
        elif isinstance(response, requests.Response):
            return response.text

    def download(self, source: str, destination: Path) -> Path | bool:
        response, error = get_from_url(source)
        if error:
            self.message = "Error downloading fms file from SimBrief"
            self.error = error
            return False
        elif isinstance(response, requests.Response):
            try:
                with open(destination, 'wb') as f:
                    f.write(response.content)
            except Exception as e:
                print(f"*** simbrief2zibo error saving FP file: {e.args[0]}")
                self.message = "Error writing FP file"
                self.error = e.args[0]
                return False
        return destination

    def process(self, text: str):
        """ only XML now"""
        data = ET.fromstring(text)

        request_id = data.find('params').find('request_id').text
        if self.request_id == request_id:
            # no new OFP
            self.message = "No new OFP available"
            return
        ofp = shrink_xml(data)
        fp_filename = self.find_or_retrieve_fp(ofp)
        if fp_filename:
            self.request_id = request_id
            self.coroute_filename = fp_filename
            parsed = self.parse_ofp(ofp)
            if self.create_xml_file(ofp, parsed):
                self.message = "All set!"
                # get more info
                callsign = ofp.find('atc').find('callsign').text
                weights = ofp.find('weights')
                u = ofp.find('params').find('units').text
                oew = weights.find('oew').text
                cargo = weights.find('cargo').text
                payload = weights.find('payload').text
                zfw = weights.find('est_zfw').text
                tow = weights.find('est_tow').text
                ldw = weights.find('est_ldw').text
                self.fp_info = {
                    'origin': self.origin.upper().strip(),
                    'destination': self.destination.upper().strip(),
                    'callsign': callsign,
                    'co route': fp_filename,
                    'oew': f"{oew} {u} ({weight_transform(oew, u)})",
                    'pax': f"{weights.find('pax_count_actual').text}",
                    'cargo': f"{cargo} {u} ({weight_transform(cargo, u)})",
                    'payload': f"{payload} {u} ({weight_transform(payload, u)})",
                    'zfw': f"{zfw} {u} ({weight_transform(zfw, u)})",
                    'tow': f"{tow} {u} ({weight_transform(tow, u)})",
                    'ldw': f"{ldw} {u} ({weight_transform(ldw, u)})"
                }

    def parse_ofp(self, ofp: ET.Element) -> dict:
        """
        LIDO: \n400 288/021 -54  400 320/020 -54  400 332/028 -55  350 330/022 -44\n380 272/019 -50  380 310/016 -50  380 333/024 -51  310 343/025 -34\n360 285/017 -46  360 319/017 -46  360 331/023 -46  200 005/009 -10\n340 301/016 -42  340 326/019 -42  340 328/021 -42  150 297/004 +02\n320 313/019 -36  320 328/021 -37  320 332/024 -37  100 258/001 +11
        """
        layout = ofp.find('params').find('ofp_layout').text
        fix = ofp.find('navlog').findall('fix')[-1]
        if fix.find('ident').text == ofp.find('destination').find('icao_code').text:
            dest_isa = int(fix.find('oat_isa_dev').text)
        else:
            # use avg isa dev
            dest_isa = int(ofp.find('general').find('avg_temp_dev').text)
        dest_metar = ofp.find('destination').find('metar').text

        return {
            'dest_isa': dest_isa,
            'dest_metar': dest_metar,
            'winds': extract_descent_winds(ofp, layout=layout)
        }

    def find_or_retrieve_fp(self, ofp: ET.Element) -> str | bool:

        orig = ofp.find('origin')
        dest = ofp.find('destination')
        self.origin = orig.find('icao_code').text
        self.destination = dest.find('icao_code').text
        if not (self.origin and self.destination):
            return False

        # ver. 5.6+ fms file not needed for UPLINK feature anymore
        # user could have downloaded a fms file from Navigraph, which contains SID and STAR.
        # There could be a fmx file from FMC. it's not usable for UPLINK, just for CO ROUTE
        # look for a CO ROUTE file
        file = None
        recent = datetime.now() - timedelta(days=DAYS)
        files = [
            f for f in self.path.iterdir()
            if f.suffix in ('.fms', '.fmx')
            and f.stem.startswith(self.origin)
            and self.destination in f.stem
            and datetime.fromtimestamp(f.stat().st_ctime) > recent
        ]
        if files:
            # user already has a FP for this OFP
            file = max(files, key=lambda x: x.stat().st_ctime)
        else:
            # need to download the fms file from SimBrief
            file = Path(self.path, self.origin + self.destination + '.fms')
            fms_downloads = ofp.find('fms_downloads')
            self.fp_link = fms_downloads.find('directory').text + fms_downloads.find('xpe').find('link').text
            result = self.download(self.fp_link, file)
            if not result:
                return False
            dep, arr = extract_dep_arr(ofp)
            if dep or arr:
                insert_dep_arr(file, dep, arr)

        return file.stem

    def create_xml_file(self, ofp: ET.Element, data: dict, filename: str = uplink_filename) -> bool:
        """we need to recreate the plan_html parts we use as in LIDO format"""

        # const
        summary_tag = '''<div style="line-height:14px;font-size:13px"><pre><!--BKMK///OFP///0--><!--BKMK///Summary and Fuel///1--><b>[ OFP ]\n--------------------------------------------------------------------</b>\nOFP 1\n\n'''
        wind_tag = '''<h2 style="page-break-after: always;"> </h2><!--BKMK///Wind Information///1-->--------------------------------------------------------------------\n WIND INFORMATION \nDESCENT\n'''
        wx_tag = '''<h2 style="page-break-after: always;"> </h2><!--BKMK///Airport WX List///0--><b>[ Airport WX List ]\n--------------------------------------------------------------------</b>\nDestination:\n'''

        dest_isa = f"AVG ISA       {'M' if data['dest_isa'] < 0 else 'P'}{abs(data['dest_isa']):03d}\n\n"
        winds = '\n'.join([' '.join([e for e in el]) for el in data['winds']]) + '\n\n'
        parts = data['dest_metar'].split()[1:]
        parts[0] = parts[0].replace('Z', ' ')
        dest_metar = f"{self.destination}\nSA  {' '.join(parts)}\n"

        plan_html = ofp.find('text').find('plan_html')

        plan_html.text = summary_tag + dest_isa + wind_tag + winds + wx_tag + dest_metar

        filename = filename + '.xml'
        file = Path(self.path, filename)
        tree = ET.ElementTree(ofp)
        try:
            tree.write(file, encoding='utf-8', xml_declaration=True)
            return True
        except Exception as e:
            self.message = f"Error writing {filename}"
            self.error = e
            return False

    def delete_old_xml_files(self):
        for f in self.path.iterdir():
            if f.stem == self.uplink_filename:
                f.unlink()


class Atis(object):

    def __init__(self, icao: str) -> None:
        self.icao = icao
        self.error = None
        self.atis_info = None
        self.result = False

    @property
    def url(self) -> str:
        return f"https://atis.rudicloud.com/a/{self.icao}"

    @staticmethod
    def run(icao: str) -> dict:
        """
        return
        {'error', 'request_id', 'coroute_filename', 'message', 'fp_info'}
        """

        a = Atis(icao)
        response = a.query()
        if response and not a.error:
            a.process(response)
        result = {
            'error': a.error,
            'atis': a.atis_info
        }
        return result

    def query(self) -> str | None:
        response, error = get_from_url(self.url)
        if error:
            if error == 500:
                self.message = "Error: ICAO does not exist"
            else:
                self.message = "Error trying to connect to DATIS server"
            self.error = error
        elif isinstance(response, requests.Response):
            return response.text

    def process(self, data: str) -> None:

        if '<div class="atis-text">' not in data:
            # DATIS not available
            self.atis_info = f"D-ATIS not available for {self.icao}"
            return
        atis = data.split('<div class="atis-text">')[1].split('</div>')[0]\
            .replace('\n\t', ' ').replace('\r\n', ' ').strip()
        self.atis_info = atis


def shrink_xml(data: ET.Element) -> ET.Element:
    tag_list = [
        'fetch', 'aircraft', 'times', 'impacts', 'crew', 'notams', 'weather', 'sigmets', 'tracks', 
        'database_updates', 'files', 'images', 'links', 'prefile', 
        'vatsim_prefile', 'ivao_prefile', 'pilotedge_prefile', 'poscon_prefile', 'map_data'
    ]

    for tag in tag_list:
        results = data.findall(tag)
        if results:
            # print(f"found {tag}")
            for el in results:
                data.remove(el)
    for tag in ('origin', 'destination', 'alternate'):
        el = data.find(tag)
        if el:
            taf = el.find('taf')
            if taf:
                el.remove(taf)
            for notam in el.findall('notam'):
                el.remove(notam)
            for taf in el.findall('taf'):
                el.remove(taf)

    return data


def extract_dep_arr(ofp: ET.Element) -> tuple[list, list]:
    """ Try to extract SID and STAR procedures from xml file
        to mimic Navigraph fms file """
    orig = ofp.find('origin')
    dest = ofp.find('destination')
    dep_icao = orig.find('icao_code').text
    arr_icao = dest.find('icao_code').text
    rte = ofp.find('api_params').find('route').text.split()
    dep = []
    arr = []

    if rte:
        # Departure
        dep_rwy = orig.find('plan_rwy').text
        if dep_rwy:
            # we have a dep. rwy
            dep.append(f"DEPRWY RW{dep_rwy}")
        if rte[0].startswith(dep_icao):
            rte.pop(0)
        if len(rte) > 1:
            if '.' in rte[0]:
                # we should have SID.TRANS
                sid, trans = rte[0].split('.')
                dep.extend([
                    f"SID {sid}",
                    f"SIDTRANS {trans}"
                ])
            elif len([c for c in rte[0] if c.isdigit()]) == 1:
                # we should have SID
                dep.append(f"SID {rte[0]}")
                if not any(s.isdigit() for s in rte[1]):
                    dep.append(f"SIDTRANS {rte[1]}")
        # Arrival
        arr_rwy = dest.find('plan_rwy').text
        if arr_rwy:
            arr.append(f"DESRWY RW{arr_rwy}")
        des = None
        if rte[-1].startswith(arr_icao):
            des = rte.pop(-1)
        if len(rte) > 3 and rte[0] != rte[-1]:
            if '.' in rte[-1]:
                # we should have STAR.TRANS
                star, trans = rte[-1].split('.')
                arr.extend([
                    f"STAR {star}",
                    f"STARTRANS {trans}"
                ])
            elif (len([c for c in rte[-1] if c.isdigit()]) == 1 or rte[-1].endswith(arr_rwy)):
                # we should have STAR
                arr.append(f"STAR {rte[-1]}")
                if not any(s.isdigit() for s in rte[-2]):
                    arr.append(f"STARTRANS {rte[-2]}")
        if des and '/' in des:
            _, app = des.split('/')
            arr.append(f"APP {app}")
    return dep, arr


def insert_dep_arr(file: Path, dep: list, arr: list) -> None:
    if dep or arr:
        # insert Navigraph format details in fms file
        with open(file, mode='r+', encoding='utf-8') as f:
            content = f.readlines()
            if dep:
                idx = content.index(next(l for l in content if l.startswith('ADEP')))
                for line in dep:
                    idx += 1
                    content.insert(idx, line + '\n')
            if arr:
                idx = content.index(next(l for l in content if l.startswith('ADES')))
                for line in arr:
                    idx += 1
                    content.insert(idx, line + '\n')
            f.seek(0)
            f.writelines(content)


def extract_descent_winds(ofp: ET.Element, layout: str) -> list:
    """
    Descent wind have to be extracted from plan_html section, so it's dependant on OFP layout
    """
    source = ofp.find('text').find('plan_html').text

    if any(s in layout for s in ('RYR', 'LIDO', 'THY', 'ACA')):
        text = source.split('DESCENT')[1].split('\n\n')[0]
        lines = text.split('\n')[1:]
        return [tuple(l.split()[-3:]) for l in lines]
    elif layout == 'UAL 2018':
        text = source.split('DESCENT WINDS')[1].split('STARTFWZPAD')[0]
        lines = text.split('</tr><tr>')[1:5]
        winds = []
        for l in lines:
            table = ET.XML(f"<html> + {l} + </html>")
            rows = iter(table)
            winds.append(tuple(row.text.strip().replace('FL', '') or '+15' for row in rows))
    elif layout == 'DAL':
        text = source.split('DESCENT FORECAST WINDS')[1].split('*')[0]
        lines = text.split('\n')[1:-1]
        data = list(zip(*[line.split() for line in lines]))
        idx100 = list(map(lambda x:x[0], data)).index("10000") + 1
        return [(el[0][:-2], f"{el[1][:2]}0/{el[1][-3:]}", '+15') for el in data][:idx100]
    elif layout == 'SWA':
        text = source.split('DESCENT WINDS')[1].split('\n\n')[0]
        lines = text.strip().split('\n')
        data = list(zip(*[line.split() for line in lines]))
        return [
            (
                el[0][:-2],
                f"{el[1][:2]}0{el[1][2:6]}", 
                f"{'+' if 'P' in el[1] else '-'}{el[1][-2:]}"
            )
            for el in data
        ]
    elif layout == 'KLM':
        text = source.split('CRZ ALT')[1].split('DEFRTE')[0]
        lines = lines = text.replace('FL', '').split('\n')[:3]
        return [(*l.split()[-2:], '+15') for l in lines]
    else:
        # AAL, QFA have no descent winds in OFP
        # AFR, DLH, UAE, JZA, JBU, GWI, EZY, ETD, EIN, BER, BAW, AWE have no 738 or are not operative
        return [('', '', '')]*5
    return winds


def str2int(string: str) -> int:
    v = string.strip()
    if len(v) == 0:
        return 0
    sign = 1
    if not v.isdigit():
        if v[1:].isdigit() and v[0] in ('-', '+'):
            if v[0] == '-':
                sign = -1
            v = v[1:]
        else:
            raise ValueError(f"Input string {v} is not a valid integer.")
    return int(v) * sign


def weight_transform(weight: str, unit: str) -> str:
    if unit == 'kgs':
        t = 'lbs'
        m = 2.205
    else:
        t = 'kgs'
        m = 0.4535
    return f"{round(str2int(weight) * m)} {t}"


class EasyCommand:
    """
    Creates a command with an assigned callback with arguments
    """

    def __init__(self, plugin, command, function, args=False, description=''):
        command = f"{plugin_command_origin}/{command}"
        self.command = xp.createCommand(command, description)
        self.commandCH = self.commandCHandler
        xp.registerCommandHandler(self.command, self.commandCH, 1, 0)

        self.function = function
        self.args = args
        self.plugin = plugin

    def commandCHandler(self, inCommand, inPhase, inRefcon):
        if inPhase == 0:
            if self.args:
                if type(self.args).__name__ == 'tuple':
                    self.function(*self.args)
                else:
                    self.function(self.args)
            else:
                self.function()
        return 0

    def destroy(self):
        xp.unregisterCommandHandler(self.command, self.commandCH, 1, 0)


class FloatingWidget(object):

    LINE = FONT_HEIGHT + 4
    WIDTH = 240
    HEIGHT = 320
    HEIGHT_MIN = 100
    MARGIN = 10
    HEADER = 16

    left, top, right, bottom = 0, 0, 0, 0

    def __init__(self, title: str, x: int, y: int, width: int = WIDTH, height: int = HEIGHT) -> None:

        # main window internal margins
        self.left, self.top, self.right, self.bottom = (
            x + self.MARGIN,
            y - self.HEADER,
            x + width - self.MARGIN,
            y - height + self.MARGIN
        )
        self.pilot_info_subwindow = None
        self.info_line = None
        self.content_widget = {
            'subwindow': None,
            'title': None,
            'lines': []
        }

        # main widget
        self.widget = xp.createWidget(
            x, y, x + width, y - height, 
            1, title, 1, 0, xp.WidgetClass_MainWindow
        )
        xp.setWidgetProperty(self.widget, xp.Property_MainWindowHasCloseBoxes, 1)
        xp.setWidgetProperty(self.widget, xp.Property_MainWindowType, xp.MainWindowStyle_Translucent)

        # window popout button
        self.popout_button = xp.createWidget(
            self.right - FONT_WIDTH, self.top, self.right, self.top - FONT_HEIGHT,
            1, "", 0, self.widget, xp.WidgetClass_Button
        )
        xp.setWidgetProperty(self.popout_button, xp.Property_ButtonType, xp.LittleUpArrow)

        # set underlying window
        self.window = xp.getWidgetUnderlyingWindow(self.widget)
        xp.setWindowTitle(self.window, title)

        self.top -= 26

    @property
    def content_width(self) -> int:
        l, _, r, _ = self.get_subwindow_margins()
        return r - l

    @staticmethod
    def cr() -> int:
        return FloatingWidget.LINE + FloatingWidget.MARGIN

    @staticmethod
    def check_widget_descriptor(widget, text: str) -> None:
        if text not in xp.getWidgetDescriptor(widget):
            xp.setWidgetDescriptor(widget, text)
            xp.showWidget(widget)

    @classmethod
    def create_window(cls, title: str, x: int, y: int, width: int = WIDTH, height: int = HEIGHT) -> FloatingWidget:
        return cls(title, x, y, width, height)

    def get_height(self, lines: int | None = None) -> int:
        if not lines:
            return self.top - self.bottom
        else:
            return self.LINE*lines + 2*self.MARGIN

    def get_subwindow_margins(self, lines: int | None = None) -> tuple[int, int, int, int]:
        height = self.get_height(lines)
        return self.left + self.MARGIN, self.top - self.MARGIN, self.right - self.MARGIN, self.top - height + self.MARGIN

    def add_info_line(self) -> None:
        if not self.info_line:
            self.info_line = xp.createWidget(
                self.left, self.top, self.right, self.top - self.LINE,
                1, "", 0, self.widget, xp.WidgetClass_Caption
            )
            xp.setWidgetProperty(self.info_line, xp.Property_CaptionLit, 1)
            self.top -= self.cr()

    def check_info_line(self, message: str) -> None:
        if xp.getWidgetDescriptor(self.info_line) != message:
            xp.setWidgetDescriptor(self.info_line, message)

    def add_button(self, text: str, subwindow: bool = False, align: str = 'left'):
        width = int(xp.measureString(FONT, text)) + FONT_WIDTH*4
        if align == 'left':
            l, r = self.left + subwindow*self.MARGIN, self.left + width + subwindow*self.MARGIN
        else:
            l, r = self.right - width - subwindow*self.MARGIN, self.right - subwindow*self.MARGIN
        return xp.createWidget(
            l, self.top, r, self.top - self.LINE,
            0, text, 0, self.widget, xp.WidgetClass_Button
        )

    def add_subwindow(self, lines: int | None = None):
        height = self.get_height(lines)
        return xp.createWidget(
            self.left, self.top, self.right, self.top - height,
            1, "", 0, self.widget, xp.WidgetClass_SubWindow
        )

    def add_user_info_widget(self) -> None:
        # user info subwindow
        self.pilot_info_subwindow = self.add_subwindow(lines=1)
        l, t, r, b = self.get_subwindow_margins(lines=1)
        # user info widgets
        caption = xp.createWidget(
            l, t, l + 90, b,
            1, 'Simbrief PilotID:', 0, self.widget, xp.WidgetClass_Caption
        )
        self.pilot_id_input = xp.createWidget(
            l + 88, t, l + 145, b,
            1, "", 0, self.widget, xp.WidgetClass_TextField
        )
        xp.setWidgetProperty(self.pilot_id_input, xp.Property_MaxCharacters, 10)
        self.pilot_id_caption = xp.createWidget(
            l + 88, t, l + 145, b,
            1, "", 0, self.widget, xp.WidgetClass_Caption
        )
        self.save_button = xp.createWidget(
            l + 148, t, r, b,
            1, "SAVE", 0, self.widget, xp.WidgetClass_Button
        )
        self.edit_button = xp.createWidget(
            l + 148, t, r, b,
            1, "CHANGE", 0, self.widget, xp.WidgetClass_Button
        )
        self.top = b - self.MARGIN*2

    def add_content_widget(self, title: str = "", lines: int | None = None):
        self.content_widget['subwindow'] = self.add_subwindow(lines=lines)
        l, t, r, b = self.get_subwindow_margins()
        if len(title):
            # add title line
            self.content_widget['title'] = xp.createWidget(
                l, t, r, t - self.LINE,
                1, title, 0, self.widget, xp.WidgetClass_Caption
            )
            t -= self.cr()
        # add content lines
        while t > b:
            self.content_widget['lines'].append(
                xp.createWidget(l, t, r, t - self.LINE,
                                1, '--', 0, self.widget, xp.WidgetClass_Caption)
            )
            t -= self.LINE

    def show_content_widget(self):
        if not xp.isWidgetVisible(self.content_widget['subwindow']):
            xp.showWidget(self.content_widget['subwindow'])
            if self.content_widget['title']:
                xp.showWidget(self.content_widget['title'])
            for el in self.content_widget['lines']:
                xp.showWidget(el)

    def hide_content_widget(self):
        if xp.isWidgetVisible(self.content_widget['subwindow']):
            xp.hideWidget(self.content_widget['subwindow'])
            if self.content_widget['title']:
                xp.hideWidget(self.content_widget['title'])
            for el in self.content_widget['lines']:
                xp.hideWidget(el)

    def check_content_widget(self, lines: list[tuple[str, str] or str]):
        content = self.content_widget['lines']
        for i, el in enumerate(lines):
            if i < len(content):
                text = str(el) if not isinstance(el, tuple) else  f"{el[0].upper()}: {el[1]}"
                if not text in xp.getWidgetDescriptor(content[i]):
                    xp.setWidgetDescriptor(content[i], text)

    def populate_content_widget(self, lines: list[tuple[str, str] or str]):
        content = self.content_widget['lines']
        for i, el in enumerate(lines):
            text = str(el) if not isinstance(el, tuple) else  f"{el[0].upper()}: {el[1]}"
            xp.setWidgetDescriptor(content[i], text)

    def clear_content_widget(self):
        content = self.content_widget['lines']
        for el in content:
            xp.setWidgetDescriptor(el, "--")

    def switch_window_position(self):
        if xp.windowIsPoppedOut(self.window):
            xp.setWindowPositioningMode(self.window, xp.WindowPositionFree)
            xp.setWidgetProperty(self.popout_button, xp.Property_ButtonType, xp.LittleUpArrow)
            xp.setWidgetProperty(self.widget, xp.Property_MainWindowHasCloseBoxes, 1)
        else:
            xp.setWindowPositioningMode(self.window, xp.WindowPopOut)
            xp.setWidgetProperty(self.popout_button, xp.Property_ButtonType, xp.LittleDownArrow)
            xp.setWidgetProperty(self.widget, xp.Property_MainWindowHasCloseBoxes, 0)

    def set_window_visible(self) -> None:
        if not xp.getWindowIsVisible(self.window):
            xp.setWidgetProperty(self.widget, xp.Property_MainWindowHasCloseBoxes, 1)
            xp.setWindowIsVisible(self.window, 1)

    def toggle_window(self) -> None:
        if not xp.getWindowIsVisible(self.window):
            self.set_window_visible()
        else:
            xp.setWindowIsVisible(self.window, 0)

    def setup_widget(self, pilot_id: str | None = None):
        if pilot_id:
            xp.hideWidget(self.pilot_id_input)
            xp.hideWidget(self.save_button)
            xp.setWidgetDescriptor(self.pilot_id_caption, f"{pilot_id}")
            xp.showWidget(self.pilot_id_caption)
            xp.showWidget(self.edit_button)
        else:
            xp.hideWidget(self.pilot_id_caption)
            xp.hideWidget(self.edit_button)
            xp.showWidget(self.pilot_id_input)
            xp.showWidget(self.save_button)
            xp.setKeyboardFocus(self.pilot_id_input)

    def destroy(self) -> None:
        xp.destroyWidget(self.widget)
        # xp.destroyWindow(self.window)


class PythonInterface(object):

    loop_schedule = DEFAULT_SCHEDULE

    def __init__(self) -> None:
        self.plugin_name = f"{plugin_name} - {__VERSION__}"
        self.plugin_sig = plugin_sig
        self.plugin_desc = plugin_desc

        # folders init
        self.xp_root = Path(xp.getSystemPath())
        self.prefs = Path(xp.getPrefsPath()).parent
        self.plans = Path(self.prefs.parent, 'FMS plans')

        # Dref init
        self.gears_on_ground = xp.findDataRef('sim/flightmodel2/gear/on_ground')
        self.engines_burning_fuel = xp.findDataRef('sim/flightmodel2/engines/engine_is_burning_fuel')

        # app init
        self.config_file = Path(self.prefs, 'simbrief2zibo.prf')
        self.pilot_id = None  # SimBrief UserID, int
        self.async_task = False
        self.async_datis = False
        self.request_id = None  # OFP generated ID
        self.fp_info = {}  # information to display in the settings window
        self.aircraft = False
        self.acf_path = None

        # D-ATIS init
        self.datis_request = False  # D-ATIS request ICAO
        self.datis_content = []

        # status flags
        self.flight_started = False  # tracks simulation phase
        self.fp_checked = False  # tracks app phase

        # load settings
        self.load_settings()

        # widget and windows
        self.details = None
        self.datis = None
        self.details_message = ""  # text displayed in widget info_line
        self.datis_message = ""  # information to display in the D-ATIS window

        # create main menu and widget
        self.main_menu = self.create_main_menu()

        # register commands
        self.detailsWindowCMD = EasyCommand(
            self, 'details_window_toggle', 
            self.detailsWindowToggle,
            description="Toggle SimBrief2Zibo OFP details window."
        )
        if DATIS:
            self.datisWindowCMD = EasyCommand(
                self, 'datis_window_toggle', 
                self.datisWindowToggle,
                description="Toggle SimBrief2Zibo D-ATIS window."
            )
        self.OFPReloadCMD = EasyCommand(
            self, 'reload_simbrief_ofp', 
            self.OFPReload,
            description="Send a OFP request to SimBrief"
        )

    @property
    def aircraft_detected(self) -> bool:
        self.check_aircraft()
        return bool(self.aircraft)

    @property
    def engines_started(self) -> bool:
        values = []
        xp.getDatavi(self.engines_burning_fuel, values, count=2)
        return any(values)

    @property
    def on_ground(self) -> bool:
        values = []
        xp.getDatavi(self.gears_on_ground, values, count=3)
        # should be all(values) but after Zibo loading front gear appears to be in the air
        return any(values)

    @property
    def at_gate(self) -> bool:
        return self.on_ground and not self.engines_started

    def check_aircraft(self) -> None:
        _, acf_path = xp.getNthAircraftModel(0)
        if acf_path != self.acf_path:
            self.acf_path = acf_path
            acf = next((p[0] for p in AIRCRAFTS if p[1] in self.acf_path), None)
            if acf:
                self.aircraft = acf
                if 'not detected' in self.details_message:
                    self.details_message = f"{acf} detected"
            else:
                self.aircraft = False

    def check_datis_request(self):
        if self.async_datis:
            # we already started a SimBrief async instance
            if not self.async_datis.pending():
                # job ended
                self.async_datis.join()
                if isinstance(self.async_datis.result, Exception):
                    # a non managed error occurred
                    self.datis_message = "An unknown error occurred"
                    xp.log(f" *** Unmanaged error in async task {self.async_datis.pid}: {self.async_datis.result}")
                else:
                    # result: {error, atis_info}
                    error, result = self.async_datis.result.values()
                    if error:
                        # a managed error occurred
                        self.datis_message = "Error retrieving D-ATIS"
                        xp.log(f" *** D-ATIS error in async task {self.async_datis.pid}: {error}")
                    elif result:
                        # we have a valid response
                        if "D-ATIS not available" in result:
                            # no D-ATIS available for the station, no need to display D-ATIS panel
                            self.datis_message = result
                        else:
                            self.datis_message = f"{datetime.utcnow().strftime('%H%M')}Z - {self.datis_request} D-ATIS:"
                            self.datis_content = self.format_atis_info(result)
                # reset download
                self.async_datis = False
                self.datis_request = False
            else:
                # no answer yet, waiting ...
                pass
        else:
            # we need to start an async task
            self.datis_content = []
            self.datis.clear_content_widget()
            self.datis_message = "Starting D-ATIS query ..."
            self.async_datis = Async(
                Atis.run,
                self.datis_request,
            )
            self.async_datis.start()

    def create_main_menu(self):
        # create Menu
        menu = xp.createMenu('SimBrief2Zibo', handler=self.main_menu_callback)
        # add Menu Items
        xp.appendMenuItem(menu, 'OFP Details', 1)
        if DATIS:
            # add D-ATIS widget
            xp.appendMenuItem(menu, 'D-ATIS', 2)
        return menu

    def main_menu_callback(self, menuRef, menuItem):
        """Main menu Callback"""
        if menuItem == 1:
            if not self.details:
                self.create_details_window(100, 400)
            else:
                self.details.set_window_visible()
        if menuItem == 2:
            if not self.datis:
                self.create_datis_window(100, 800)
            else:
                self.datis.set_window_visible()

    def create_details_window(self, x: int = 100, y: int = 400) -> None:

        # main window
        self.details = FloatingWidget.create_window(f"SimBrief2Zibo {__VERSION__}", x, y, width=DETAILS_WIDTH)

        # PilotID sub window
        self.details.add_user_info_widget()

        # info message line
        self.details.add_info_line()

        # reload OFP button
        self.details.reload_button = self.details.add_button('RELOAD', align='right')


        self.details.top -= self.details.cr()
        # OFP info sub window
        self.details.add_content_widget(title='OFP info:')

        self.details.setup_widget(self.pilot_id)

        # Register our widget handler
        self.settingsWidgetHandlerCB = self.detailsWidgetHandler
        xp.addWidgetCallback(self.details.widget, self.settingsWidgetHandlerCB)

    def create_datis_window(self, x: int = 100, y: int = 800) -> None:

        # main window
        self.datis = FloatingWidget.create_window("D-ATIS widget", x, y, width=ATIS_WIDTH)

        # Buttons sub window
        self.datis.add_subwindow(lines=1)
        l, t, r, b = self.datis.get_subwindow_margins(lines=1)
        self.datis.top = t
        self.datis.dep_button = self.datis.add_button("ORIG", subwindow=True)
        self.datis.arr_button = self.datis.add_button("DEST", subwindow=True, align='right')
        self.datis.top = b - self.datis.MARGIN

        # info message line
        self.datis.add_info_line()

        # add content widget
        self.datis.add_content_widget()

        # Register our widget handler
        self.atisWidgetHandlerCB = self.datisWidgetHandler
        xp.addWidgetCallback(self.datis.widget, self.atisWidgetHandlerCB)

    def detailsWidgetHandler(self, inMessage, inWidget, inParam1, inParam2):
        if not self.details:
            return 1

        self.details.check_info_line(self.details_message)

        if self.aircraft_detected and self.fp_checked and self.fp_info:
            self.details.check_content_widget(lines=list(self.fp_info.items())[2:])
            self.details.show_content_widget()
        else:
            self.details.hide_content_widget()

        if self.fp_checked and not self.flight_started:
            xp.showWidget(self.details.reload_button)
        else:
            xp.hideWidget(self.details.reload_button)

        if inMessage == xp.Message_CloseButtonPushed:
            if self.details.window:
                xp.setWindowIsVisible(self.details.window, 0)
                return 1

        if inMessage == xp.Msg_PushButtonPressed:
            if inParam1 == self.details.popout_button:
                self.details.switch_window_position()
            if inParam1 == self.details.save_button:
                self.save_settings()
                return 1
            if inParam1 == self.details.edit_button:
                xp.setWidgetDescriptor(self.details.pilot_id_input, f"{self.pilot_id}")
                self.pilot_id = None
                self.details.setup_widget()
                return 1
            if inParam1 == self.details.reload_button:
                self.fp_checked = False
                self.details_message = 'OFP reload requested'
                return 1
        return 0

    def datisWidgetHandler(self, inMessage, inWidget, inParam1, inParam2):
        if not self.datis:
            return 0

        self.datis.check_info_line(self.datis_message)

        if self.aircraft_detected and self.fp_info:
            self.datis.check_widget_descriptor(self.datis.dep_button, self.fp_info['origin'])
            self.datis.check_widget_descriptor(self.datis.arr_button, self.fp_info['destination'])
            if self.datis_content:
                self.datis.check_content_widget(self.datis_content)
                self.datis.show_content_widget()
            else:
                self.datis.hide_content_widget()

            if self.datis_request:
                # we have an D-ATIS request
                self.check_datis_request()
        else:
            self.datis.hide_content_widget()
            xp.hideWidget(self.datis.dep_button)
            xp.hideWidget(self.datis.arr_button)

        # manage close window button
        if inMessage == xp.Message_CloseButtonPushed:
            if self.datis.window:
                xp.setWindowIsVisible(self.datis.window, 0)
            return 1

        # manage widget buttons
        if inMessage == xp.Msg_PushButtonPressed:
            if inParam1 == self.datis.popout_button:
                self.datis.switch_window_position()
            else:
                icao = self.fp_info['origin' if inParam1 == self.datis.dep_button else 'destination']
                xp.log(f"ATIS request: {icao}")
                self.datis_request = icao
            return 1

        return 0

    def detailsWindowToggle(self):
        if not self.details:
            self.create_details_window(100, 400)
        else:
            self.details.toggle_window()

    def datisWindowToggle(self):
        if not self.datis:
            self.create_datis_window(100, 800)
        else:
            self.datis.toggle_window()

    def OFPReload(self):
        if self.aircraft_detected and self.fp_checked:
            self.details_message = 'OFP reload requested'
            self.fp_checked = False

    def format_atis_info(self, string: str) -> list:
        # create lines from D-ATIS string
        width = self.datis.content_width
        words = string.split(' ')
        result = ['']
        for word in words:
            if xp.measureString(FONT, result[-1] + ' ' + word) < width:
                result[-1] += word if not result[-1] else ' ' + word
            else:
                result.append(word)
        return result

    def loopCallback(self, lastCall, elapsedTime, counter, refCon):
        """Loop Callback"""
        t = datetime.now().strftime('%H:%M:%S')
        start = perf_counter()
        if self.aircraft_detected and self.pilot_id:
            if not self.flight_started:
                if not self.fp_checked and self.at_gate:
                    # check fp
                    if self.async_task:
                        # we already started a SimBrief async instance
                        if not self.async_task.pending():
                            # job ended
                            self.async_task.join()
                            if isinstance(self.async_task.result, Exception):
                                # a non managed error occurred
                                self.details_message = "An unknown error occurred"
                                xp.log(f" *** Unmanaged error in async task {self.async_task.pid}: {self.async_task.result}")
                            else:
                                # result: {error, request_id, message, fp_info}
                                error, request_id, self.details_message, fp_info = self.async_task.result.values()
                                if error:
                                    # a managed error occurred
                                    xp.log(f" *** SimBrief error in async task {self.async_task.pid}: {error}")
                                elif fp_info:
                                    # we have a valid response
                                    self.request_id, self.fp_info = request_id, fp_info
                                    self.fp_checked = True
                                elif self.fp_info:
                                    # reload was requested, no no OFP found, we do not need to keep checking right now
                                    self.fp_checked = True
                            # reset download
                            self.async_task = False
                            self.loop_schedule = DEFAULT_SCHEDULE
                        else:
                            # no answer yet, waiting ...
                            pass
                    else:
                        # we need to start an async task
                        self.details_message = "starting SimBrief query ..."
                        self.async_task = Async(
                            SimBrief.run,
                            self.pilot_id,
                            self.plans,
                            self.request_id
                        )
                        self.async_task.start()
                        self.loop_schedule = 3
                elif not self.flight_started and not self.at_gate:
                    # flight mode, do nothing
                    self.flight_started = True
                    self.details_message = "Have a nice flight!"
                    self.loop_schedule = DEFAULT_SCHEDULE * 10
            elif self.at_gate:
                # look for a new OFP for a turnaround flight
                self.flight_started = False
                self.fp_checked = False
                self.fp_info = {}
                self.details_message = "Looking for a new OFP ..."
                self.loop_schedule = DEFAULT_SCHEDULE
        else:
            # nothing to do
            if not self.aircraft_detected:
                self.details_message = "Aircraft not detected"
            elif not self.pilot_id:
                self.details_message = "SimBrief PilotID required"
            self.loop_schedule = DEFAULT_SCHEDULE * 5

        return self.loop_schedule

    def load_settings(self) -> bool:
        if self.config_file.is_file():
            # read file
            with open(self.config_file, 'r', encoding='utf-8') as f:
                data = f.read()
            # parse file
            settings = json.loads(data)
            self.pilot_id = settings.get('settings').get('pilot_id')
            return True
        else:
            # open settings window
            return False

    def save_settings(self) -> None:
        user_id = xp.getWidgetDescriptor(self.details.pilot_id_input).strip()
        if not user_id.isdigit():
            # user gave something else in input
            self.details_message = "pilotID has to be a number"
            xp.setWidgetDescriptor(self.details.pilot_id_input, "")
        else:
            settings = {'settings': {'pilot_id': int(user_id)}}
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(settings, f)
            # check file
            self.load_settings()
            self.details_message = 'settings saved'
            self.details.setup_widget(self.pilot_id)

    def XPluginStart(self):
        return self.plugin_name, self.plugin_sig, self.plugin_desc

    def XPluginEnable(self):
        # enable features
        xp.enableFeature("XPLM_USE_NATIVE_WIDGET_WINDOWS", 1)
        # loopCallback
        self.loop = self.loopCallback
        self.loop_id = xp.createFlightLoop(self.loop, phase=1)
        xp.scheduleFlightLoop(self.loop_id, interval=DEFAULT_SCHEDULE)
        return 1

    def XPluginDisable(self):
        pass

    def XPluginStop(self):
        """Called once by X-Plane on quit (or when plugins are exiting as part of reload)"""

        # kill loop
        xp.destroyFlightLoop(self.loop_id)
        # destroy widgets
        if self.details:
            self.details.destroy()
        if self.datis:
            self.datis.destroy()
        # kill commands
        self.OFPReloadCMD.destroy()
        self.detailsWindowCMD.destroy()
        self.datisWindowCMD.destroy()
        # destroy menu
        xp.destroyMenu(self.main_menu)
        xp.log("flightloop, widget, commands, menu destroyed, exiting ...")
