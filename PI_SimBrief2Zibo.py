"""
SimBrief2Zibo
X-Plane plugin

Copyright (c) 2023, Antonio Golfari
All rights reserved.

This source code is licensed under the BSD-style license found in the
LICENSE file in the root directory of this source tree. 
"""

import os
import json
import threading

from pathlib import Path
from urllib import request, parse
from urllib.error import URLError, HTTPError
from http.client import HTTPResponse
from ssl import SSLCertVerificationError
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta
from time import perf_counter

try:
    from XPPython3 import xp
except ImportError:
    print('xp module not found')
    pass


# Version
__VERSION__ = 'v1.3.beta.1'

# Plugin parameters required from XPPython3
plugin_name = 'SimBrief2Zibo'
plugin_sig = 'xppython3.simbrief2zibo'
plugin_desc = 'Fetches latest OFP Data from SimBrief and creates the file ZIBO B738 requires'

# Other parameters
DEFAULT_SCHEDULE = 15  # positive numbers are seconds, 0 disabled, negative numbers are cycles
DAYS = 2  # how recent a fp file has to be to be considered

# widget parameters
try:
    FONT = xp.Font_Proportional
    FONT_WIDTH, FONT_HEIGHT, _ = xp.getFontDimensions(FONT)
except NameError:
    FONT_WIDTH, FONT_HEIGHT = 10, 10

LINE = FONT_HEIGHT + 4
WIDTH = 240
ATIS_WIDTH = WIDTH * 2
HEIGHT = 320
HEIGHT_MIN = 100
MARGIN = 10
HEADER = 16


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
        if not s.error:
            s.process(response)
        result = {
            'error': s.error,
            'request_id': s.request_id,
            'message': s.message,
            'fp_info': s.fp_info
        }
        return result

    def query(self, url: str) -> HTTPResponse | None:
        response = None
        try:
            response = request.urlopen(url)
        except HTTPError as e:
            if e.code == 400:
                # HTTP Error 400: Bad Request
                self.message = "Error: is your pilotID correct?"
            else:
                self.message = "Error trying to connect to SimBrief"
            self.error = e
            return
        except (SSLCertVerificationError, URLError) as e:
            # change link to unsecure protocol to avoid SSL error in some weird systems
            print(f" *** get_ofp() had to run in unsecure mode: {e}")
            try:
                parsed = parse.urlparse(url)
                parsed = parsed._replace(scheme=parsed.scheme.replace('https', 'http'))
                link = parse.urlunparse(parsed)
                response = request.urlopen(link)
            except (HTTPError, URLError) as e:
                self.message = f"Error trying to connect to SimBrief"
                self.error = e
                return
        except Exception as e:
            self.message = f"Error trying to connect to SimBrief"
            self.error = e
            return
        return response

    def download(self, source: str, destination: Path) -> Path | bool:
        try:
            result = request.urlretrieve(source, destination)
        except (SSLCertVerificationError, HTTPError, URLError):
            # change link to unsecure protocol to avoid SSL error in some weird systems
            parsed = parse.urlparse(source)
            parsed = parsed._replace(scheme=parsed.scheme.replace('https', 'http'))
            link = parse.urlunparse(parsed)
            try:
                result = request.urlretrieve(link, destination)
            except (HTTPError, URLError) as e:
                self.message = f"Error downloading fms file from SimBrief"
                self.error = e
                return False
        except Exception as e:
            self.message = f"Error downloading fms file from SimBrief"
            self.error = e
            return False
        return destination

    def process(self, response: HTTPResponse):
        """ only XML now"""
        xml_source = ET.parse(response)
        data = xml_source.getroot()

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

            # delete old XML file from the FMS plans folder
            # self.delete_old_xml_files()

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
            #! test: insert dep and arr procedures in downloaded fms file
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
        return f"https://atis.report/a/{self.icao}"

    @staticmethod
    def run(icao: str) -> dict:
        """
        return
        {'error', 'request_id', 'coroute_filename', 'message', 'fp_info'}
        """

        a = Atis(icao)
        response = a.query()
        if not a.error:
            a.process(response)
        result = {
            'error': a.error,
            'atis': a.atis_info
        }
        return result

    def query(self) -> HTTPResponse | None:
        response = None
        try:
            query = request.Request(self.url, headers={'User-Agent': 'Mozilla/5.0'})
            response = request.urlopen(query)
        except HTTPError as e:
            if e.code == 500:
                # HTTP Error 500: Internal Server Error
                self.atis_info = "Error: ICAO does not exist"
            else:
                self.atis_info = "Error trying to connect to DATIS server"
            self.error = e
            return
        except (SSLCertVerificationError, URLError) as e:
            # change link to unsecure protocol to avoid SSL error in some weird systems
            try:
                parsed = parse.urlparse(self.url)
                parsed = parsed._replace(scheme=parsed.scheme.replace('https', 'http'))
                link = parse.urlunparse(parsed)
                query = request.Request(link, headers={'User-Agent': 'Mozilla/5.0'})
                response = request.urlopen(query)
            except (HTTPError, URLError) as e:
                self.atis_info = "Error trying to connect to DATIS server"
                self.error = e
                return
        except Exception as e:
            self.atis_info = "Error trying to connect to DATIS server"
            self.error = e
            return
        return response

    def process(self, response: HTTPResponse) -> None:
        data = response.read().decode()
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
            des = rte.pop[-1]
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


def weight_transform(weight: str, unit: str) -> int:
    if unit == 'kgs':
        t = 'lbs'
        m = 2.205
    else:
        t = 'kgs'
        m = 0.4535
    return f"{round(str2int(weight) * m)} {t}"


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
        self.async_atis = False
        self.request_id = None  # OFP generated ID
        self.fp_info = {}  # information to display in the settings window

        # D-ATIS init
        self.atis_info = []  # # information to display in the D-ATIS window
        self.atis_request = False  # D-ATIS request ICAO

        # status flags
        self.flight_started = False  # tracks simulation phase
        self.fp_checked = False  # tracks app phase

        # load settings
        self.load_settings()

        # widget and windows
        self.details_widget = None
        self.details_window = None
        self.fp_info_caption = []
        self.atis_widget = None
        self.atis_window = None
        self.atis_caption = []
        self.message = ""  # text displayed in widget info_line

        # create main menu and widget
        self.main_menu = self.create_main_menu()

    @property
    def zibo_loaded(self) -> bool:
        _, acf_path = xp.getNthAircraftModel(0)
        return 'B737-800X' in acf_path

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

    def is_visible(self, element) -> bool:
        if isinstance(element, list):
            if len(element):
                return xp.isWidgetVisible(element[0])
            else:
                return False
        return xp.isWidgetVisible(element)

    def change_group_mode(self, group: list, mode: str = 'show') -> None:
        if mode == 'show' and not all(xp.isWidgetVisible(el) for el in group):
            for el in group:
                xp.showWidget(el)
        elif mode == 'hide' and any(xp.isWidgetVisible(el) for el in group):
            for el in group:
                xp.hideWidget(el)

    def hide_fp_info_widget(self) -> None:
        self.change_group_mode(self.fp_info_caption, 'hide')
        xp.hideWidget(self.fp_info_widget)

    def show_fp_info_widget(self) -> None:
        self.change_group_mode(self.fp_info_caption, 'show')
        xp.showWidget(self.fp_info_widget)

    def hide_atis_info_widget(self) -> None:
        self.change_group_mode(self.atis_caption, 'hide')

    def show_atis_info_widget(self) -> None:
        self.change_group_mode(self.atis_caption, 'show')

    def switch_details_window(self):
        if xp.windowIsPoppedOut(self.details_window):
            xp.setWindowPositioningMode(self.details_window, xp.WindowPositionFree)
            xp.setWidgetProperty(self.details_popout_button, xp.Property_ButtonType, xp.LittleUpArrow)
            xp.setWidgetProperty(self.details_widget, xp.Property_MainWindowHasCloseBoxes, 1)
        else:
            xp.setWindowPositioningMode(self.details_window, xp.WindowPopOut)
            xp.setWidgetProperty(self.details_popout_button, xp.Property_ButtonType, xp.LittleDownArrow)
            xp.setWidgetProperty(self.details_widget, xp.Property_MainWindowHasCloseBoxes, 0)

    def switch_atis_window(self):
        if xp.windowIsPoppedOut(self.atis_window):
            xp.setWindowPositioningMode(self.atis_window, xp.WindowPositionFree)
            xp.setWidgetProperty(self.atis_popout_button, xp.Property_ButtonType, xp.LittleUpArrow)
            xp.setWidgetProperty(self.atis_widget, xp.Property_MainWindowHasCloseBoxes, 1)
        else:
            xp.setWindowPositioningMode(self.atis_window, xp.WindowPopOut)
            xp.setWidgetProperty(self.atis_popout_button, xp.Property_ButtonType, xp.LittleDownArrow)
            xp.setWidgetProperty(self.atis_widget, xp.Property_MainWindowHasCloseBoxes, 0)

    def check_atis_request(self):
        if self.async_atis:
            # we already started a SimBrief async instance
            if not self.async_atis.pending():
                # job ended
                self.async_atis.join()
                if isinstance(self.async_atis.result, Exception):
                    # a non managed error occurred
                    self.atis_info = ["An unknown error occurred"]
                    xp.log(f" *** Unmanaged error in async task {self.async_atis.pid}: {self.async_atis.result}")
                else:
                    # result: {error, atis_info}
                    error, result = self.async_atis.result.values()
                    if error:
                        # a managed error occurred
                        self.atis_info = ["Error retrieving D-ATIS"]
                        xp.log(f" *** D-ATIS error in async task {self.async_atis.pid}: {error}")
                    elif result:
                        # we have a valid response
                        self.format_atis_info(result)
                        xp.log(f" --- D-ATIS Valid response: {result}")
                # reset download
                self.async_atis = False
                self.atis_request = False
            else:
                # no answer yet, waiting ...
                pass
        else:
            # we need to start an async task
            xp.log(f" ** {datetime.now().strftime('%H:%M:%S')} loop - starting new D-ATIS async ...")
            self.clear_atis_widget()
            self.atis_info = ["starting D-ATIS query ..."]
            self.async_atis = Async(
                Atis.run,
                self.atis_request,
            )
            self.async_atis.start()

    def create_main_menu(self):
        # create Menu
        menu = xp.createMenu('SimBrief2Zibo', handler=self.main_menu_callback)
        # add Menu Items
        xp.appendMenuItem(menu, 'OFP Details', 1)
        # add D-ATIS widget
        xp.appendMenuItem(menu, 'D-ATIS', 2)
        return menu

    def main_menu_callback(self, menuRef, menuItem):
        """Main menu Callback"""
        if menuItem == 1:
            if not self.details_widget:
                self.create_details_widget(100, 400)
            elif not xp.isWidgetVisible(self.details_widget):
                xp.showWidget(self.details_widget)
        if menuItem == 2:
            if not self.atis_window:
                self.create_atis_widget(100, 800)
            elif not xp.getWindowIsVisible(self.atis_window):
                xp.setWindowIsVisible(self.atis_window, 1)

    def create_details_widget(self, x: int = 100, y: int = 400):

        left, top, right, bottom = x + MARGIN, y - HEADER, x + WIDTH - MARGIN, y - HEIGHT + MARGIN

        # main window
        self.details_widget = xp.createWidget(x, y, x+WIDTH, y-HEIGHT, 1, f"SimBrief2Zibo {__VERSION__}", 1,
                                              0, xp.WidgetClass_MainWindow)
        xp.setWidgetProperty(self.details_widget, xp.Property_MainWindowHasCloseBoxes, 1)
        xp.setWidgetProperty(self.details_widget, xp.Property_MainWindowType, xp.MainWindowStyle_Translucent)

        # window popout button
        self.details_popout_button = xp.createWidget(right-FONT_WIDTH, top, right, top-FONT_HEIGHT, 1, "", 0,
                                                     self.details_widget, xp.WidgetClass_Button)
        xp.setWidgetProperty(self.details_popout_button, xp.Property_ButtonType, xp.LittleUpArrow)

        top -= 26
        # PilotID sub window
        self.pilot_id_widget = xp.createWidget(left, top, right, top - LINE - 2*MARGIN, 1, "", 0,
                                               self.details_widget, xp.WidgetClass_SubWindow)

        l, t, r, b = left + MARGIN, top - MARGIN, right - MARGIN, top - MARGIN - LINE
        caption = xp.createWidget(l, t, l + 90, b, 1, 'Simbrief PilotID:', 0,
                                  self.details_widget, xp.WidgetClass_Caption)
        self.pilot_id_input = xp.createWidget(l + 88, t, l + 145, b, 1, "", 0,
                                              self.details_widget, xp.WidgetClass_TextField)
        xp.setWidgetProperty(self.pilot_id_input, xp.Property_MaxCharacters, 10)
        self.pilot_id_caption = xp.createWidget(l + 88, t, l + 145, b, 1, "", 0,
                                                self.details_widget, xp.WidgetClass_Caption)
        self.save_button = xp.createWidget(l + 148, t, r, b, 1, "SAVE", 0,
                                           self.details_widget, xp.WidgetClass_Button)
        self.edit_button = xp.createWidget(l + 148, t, r, b, 1, "CHANGE", 0,
                                           self.details_widget, xp.WidgetClass_Button)

        t = b - MARGIN*2
        # info message line
        self.info_line = xp.createWidget(left, t, right, t - LINE, 1, "", 0,
                                         self.details_widget, xp.WidgetClass_Caption)
        xp.setWidgetProperty(self.info_line, xp.Property_CaptionLit, 1)

        t -= LINE + MARGIN
        # reload OFP button
        self.reload_button = xp.createWidget(l + 150, t, r, t - LINE, 0, "RELOAD", 0,
                                             self.details_widget, xp.WidgetClass_Button)

        t -= LINE + MARGIN
        # OFP info sub window
        self.fp_info_widget = xp.createWidget(left, t, right, bottom, 1, "", 0, self.details_widget,
                                              xp.WidgetClass_SubWindow)
        xp.setWidgetProperty(self.fp_info_widget, xp.Property_SubWindowType, xp.SubWindowStyle_SubWindow)
        t -= MARGIN
        b = bottom + MARGIN
        w = r - l
        cap = xp.createWidget(l, t, r, t - LINE, 1, 'OFP INFO:', 0,
                              self.details_widget, xp.WidgetClass_Caption)
        self.fp_info_caption.append(cap)
        t -= LINE + MARGIN
        while t > b:
            cap = xp.createWidget(l, t, r, t - LINE, 1, '--', 0,
                                  self.details_widget, xp.WidgetClass_Caption)
            self.fp_info_caption.append(cap)
            t -= LINE

        self.setup_widget()

        # set underlying window
        self.details_window = xp.getWidgetUnderlyingWindow(self.details_widget)
        xp.setWindowTitle(self.details_window, "OFP Details")

        # Register our widget handler
        self.settingsWidgetHandlerCB = self.settingsWidgetHandler
        xp.addWidgetCallback(self.details_widget, self.settingsWidgetHandlerCB)
        xp.setKeyboardFocus(self.pilot_id_input)

    def create_atis_widget(self, x: int = 100, y: int = 800):
        width = ATIS_WIDTH
        left, top, right, bottom = x + MARGIN, y - HEADER, x + width - MARGIN, y - HEIGHT + MARGIN

        # main window
        self.atis_widget = xp.createWidget(x, y, x+width, y-HEIGHT, 1, "D-ATIS widget", 1, 0, xp.WidgetClass_MainWindow)
        xp.setWidgetProperty(self.atis_widget, xp.Property_MainWindowHasCloseBoxes, 1)
        xp.setWidgetProperty(self.atis_widget, xp.Property_MainWindowType, xp.MainWindowStyle_Translucent)

        # window popout button
        self.atis_popout_button = xp.createWidget(right-FONT_WIDTH, top, right, top-FONT_HEIGHT, 1, "", 0,
                                                  self.atis_widget, xp.WidgetClass_Button)
        xp.setWidgetProperty(self.atis_popout_button, xp.Property_ButtonType, xp.LittleUpArrow)

        top -= 26
        # Buttons sub window
        self.atis_subwindow = xp.createWidget(left, top, right, top - LINE - 2*MARGIN, 1, "", 0,
                                              self.atis_widget, xp.WidgetClass_SubWindow)
        l, t, r, b = left + MARGIN, top - MARGIN, right - MARGIN, top - LINE
        self.dep_atis_button = xp.createWidget(l, t, l+100, b, 1, "DEP", 0,
                                               self.atis_widget, xp.WidgetClass_Button)
        self.arr_atis_button = xp.createWidget(r-100, t, r, b, 1, "ARR", 0,
                                               self.atis_widget, xp.WidgetClass_Button)

        t = b - LINE - MARGIN
        b = bottom + LINE
        while t > b:
            cap = xp.createWidget(left, t, right, t - LINE, 1, '--', 0,
                                  self.atis_widget, xp.WidgetClass_Caption)
            xp.setWidgetProperty(cap, xp.Property_CaptionLit, 1)
            self.atis_caption.append(cap)
            t -= LINE

        # set underlying window
        self.atis_window = xp.getWidgetUnderlyingWindow(self.atis_widget)
        xp.setWindowTitle(self.atis_window, "D-ATIS widget")

        # Register our widget handler
        self.atisWidgetHandlerCB = self.atisWidgetHandler
        xp.addWidgetCallback(self.atis_widget, self.atisWidgetHandlerCB)

    def settingsWidgetHandler(self, inMessage, inWidget, inParam1, inParam2):
        if xp.getWidgetDescriptor(self.info_line) != self.message:
            xp.setWidgetDescriptor(self.info_line, self.message)

        if self.zibo_loaded and self.fp_checked and self.fp_info:
            if not any(self.fp_info.get('zfw') in xp.getWidgetDescriptor(el) for el in self.fp_info_caption):
                self.populate_info_widget()
            if not self.is_visible(self.fp_info_widget):
                self.show_fp_info_widget()
        else:
            self.hide_fp_info_widget()

        if self.fp_checked and not self.flight_started:
            xp.showWidget(self.reload_button)
        else:
            xp.hideWidget(self.reload_button)

        if inMessage == xp.Message_CloseButtonPushed:
            if self.details_widget:
                xp.hideWidget(self.details_widget)
                return 1

        if inMessage == xp.Msg_PushButtonPressed:
            if inParam1 == self.details_popout_button:
                self.switch_details_window()
            if inParam1 == self.save_button:
                self.save_settings()
                return 1
            if inParam1 == self.edit_button:
                xp.setWidgetDescriptor(self.pilot_id_input, f"{self.pilot_id}")
                self.pilot_id = None
                self.setup_widget()
                return 1
            if inParam1 == self.reload_button:
                self.fp_checked = False
                self.message = 'OFP reload requested'
                return 1
        return 0

    def setup_widget(self):
        if self.pilot_id:
            xp.hideWidget(self.pilot_id_input)
            xp.hideWidget(self.save_button)
            xp.setWidgetDescriptor(self.pilot_id_caption, f"{self.pilot_id}")
            xp.showWidget(self.pilot_id_caption)
            xp.showWidget(self.edit_button)
        else:
            xp.hideWidget(self.pilot_id_caption)
            xp.hideWidget(self.edit_button)
            xp.showWidget(self.pilot_id_input)
            xp.showWidget(self.save_button)

    def atisWidgetHandler(self, inMessage, inWidget, inParam1, inParam2):
        if self.zibo_loaded and self.fp_info:
            if self.fp_info['origin'] not in xp.getWidgetDescriptor(self.dep_atis_button):
                xp.setWidgetDescriptor(self.dep_atis_button, self.fp_info['origin'])
                xp.showWidget(self.dep_atis_button)
            if self.fp_info['destination'] not in xp.getWidgetDescriptor(self.arr_atis_button):
                xp.setWidgetDescriptor(self.arr_atis_button, self.fp_info['destination'])
                xp.showWidget(self.arr_atis_button)
            if self.atis_info:
                if not any(self.atis_info[0] == xp.getWidgetDescriptor(el) for el in self.atis_caption):
                    self.populate_atis_widget()
                if not self.is_visible(self.atis_caption):
                    self.show_atis_info_widget()
            else:
                self.hide_atis_info_widget()

            if self.atis_request:
                # we have an ATIS request
                self.check_atis_request()
        else:
            self.hide_atis_info_widget()
            xp.hideWidget(self.dep_atis_button)
            xp.hideWidget(self.arr_atis_button)

        # manage close window button
        if inMessage == xp.Message_CloseButtonPushed:
            if self.atis_window:
                xp.setWindowIsVisible(self.atis_window, 0)
            return 1

        # manage widget buttons
        if inMessage == xp.Msg_PushButtonPressed:
            if inParam1 == self.atis_popout_button:
                self.switch_atis_window()
            else:
                icao = self.fp_info['origin' if inParam1 == self.dep_atis_button else 'destination']
                xp.log(f"ATIS request: {icao}")
                self.atis_request = icao
            return 1

        return 0

    def populate_info_widget(self) -> None:
        for i, (k, v) in enumerate(list(self.fp_info.items())[2:], 1):
            xp.setWidgetDescriptor(self.fp_info_caption[i], f"{k.upper()}: {v}")

    def format_atis_info(self, string: str) -> None:
        # create lines from D-ATIS string
        width = ATIS_WIDTH - 2 * MARGIN
        words = string.split(' ')
        result = ['']
        for word in words:
            if xp.measureString(FONT, result[-1] + ' ' + word) < width:
                result[-1] += word if not result[-1] else ' ' + word
            else:
                result.append(word)
        self.atis_info = result

    def populate_atis_widget(self) -> None:
        caption = len(self.atis_caption)
        for i, el in enumerate(self.atis_info):
            if i < caption:
                xp.setWidgetDescriptor(self.atis_caption[i], el)

    def clear_atis_widget(self) -> None:
        for line in self.atis_caption:
            xp.setWidgetDescriptor(line, '--')

    def loopCallback(self, lastCall, elapsedTime, counter, refCon):
        """Loop Callback"""
        t = datetime.now().strftime('%H:%M:%S')
        start = perf_counter()
        if self.zibo_loaded and self.pilot_id:
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
                                self.message = f"An unknown error occurred"
                                xp.log(f" *** Unmanaged error in async task {self.async_task.pid}: {self.async_task.result}")
                            else:
                                # result: {error, request_id, message, fp_info}
                                error, request_id, self.message, fp_info = self.async_task.result.values()
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
                        self.message = "starting SimBrief query ..."
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
                    self.message = "Have a nice flight!"
                    self.loop_schedule = DEFAULT_SCHEDULE * 10
            elif self.at_gate:
                # look for a new OFP for a turnaround flight
                self.flight_started = False
                self.fp_checked = False
                self.fp_info = {}
                self.message = "Looking for a new OFP ..."
                self.loop_schedule = DEFAULT_SCHEDULE
        else:
            # nothing to do
            if not self.zibo_loaded:
                self.message = "Zibo not detected"
            elif not self.pilot_id:
                self.message = "SimBrief PilotID required"
            self.loop_schedule = DEFAULT_SCHEDULE * 5

        return self.loop_schedule

    def load_settings(self) -> bool:
        if self.config_file.is_file():
            # read file
            with open(self.config_file, 'r') as f:
                data = f.read()
            # parse file
            settings = json.loads(data)
            self.pilot_id = settings.get('settings').get('pilot_id')
            return True
        else:
            # open settings window
            return False

    def save_settings(self):
        user_id = xp.getWidgetDescriptor(self.pilot_id_input).strip()
        if not user_id.isdigit():
            # user gave something else in input
            self.message = "pilotID has to be a number"
            xp.setWidgetDescriptor(self.pilot_id_input, "")
        else:
            settings = {'settings': {'pilot_id': int(user_id)}}
            with open(self.config_file, 'w') as f:
                json.dump(settings, f)
            # check file
            self.load_settings()
            self.message = 'settings saved'
            self.setup_widget()

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
        # Called once by X-Plane on quit (or when plugins are exiting as part of reload)
        xp.destroyFlightLoop(self.loop_id)
        xp.destroyWidget(self.details_widget)
        xp.destroyWindow(self.details_window)
        xp.destroyWidget(self.atis_widget)
        xp.destroyWindow(self.atis_window)
        xp.destroyMenu(self.main_menu)
        xp.log("flightloop, widget, menu destroyed, exiting ...")
