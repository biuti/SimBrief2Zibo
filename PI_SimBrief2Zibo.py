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
from ssl import SSLCertVerificationError
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta
from time import perf_counter

try:
    from XPPython3 import xp
except ImportError:
    pass


# Version
__VERSION__ = 'v1.0'

# Plugin parameters required from XPPython3
plugin_name = 'SimBrief2Zibo'
plugin_sig = 'xppython3.simbrief2zibo'
plugin_desc = 'Fetches latest OFP Data from SimBrief and creates the file ZIBO B738 requires'

# Other parameters
DEFAULT_SCHEDULE = 15  # positive numbers are seconds, 0 disabled, negative numbers are cycles
DAYS = 2  # how recent a fp file has to be to be considered

# widget parameters
font = xp.Font_Proportional
_, line_height, _ = xp.getFontDimensions(font)
WIDTH = 250
HEIGHT = 280
HEIGHT_MIN = 100
MARGIN = 10
LINE = line_height + 4
HEADER = 32


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

    def __init__(self, pilot_id: str, path: Path, request_id: str = None) -> None:
        self.pilot_id = pilot_id
        self.path = path
        self.request_id = request_id
        self.ofp = None
        self.origin = None  # Departure ICAO
        self.destination = None  # Destination ICAO
        self.fp_link = None  # link to the SimBrief fms file
        self.fp_filename = None  # fms/fmx filename
        self.error = None
        self.message = None
        self.fp_info = None
        self.result = False

    @property
    def url(self) -> str | bool:
        return f"https://www.simbrief.com/api/xml.fetcher.php?userid={self.pilot_id}&json=1"

    @staticmethod
    def run(pilot_id: str, path: Path, request_id=None) -> tuple[Exception | None, dict]:
        """
        return
        {'error', 'request_id', 'fp_filename', 'message', 'fp_info'}
        """

        s = SimBrief(pilot_id, path, request_id)
        response = s.query(s.url)
        if not s.error:
            s.process(response)
        result = {
            'error': s.error,
            'request_id': s.request_id,
            'message': s.message,
            'fp_info': s.fp_info
        }
        return result

    def query(self, url: str) -> dict | None:
        response = None
        try:
            response = request.urlopen(url)
        except HTTPError as e:
            if e.code == 400:
                # HTTP Error 400: Bad Request
                self.message = f"Error: is your pilotID correct?"
            else:
                self.message = f"Error trying to connect to SimBrief"
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
        return json.loads(response.read())

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

    def process(self, response: dict):
        request_id = response.get('params').get('request_id')
        if self.request_id == request_id:
            # no new OFP
            self.message = "No new OFP available"
            return
        ofp = response
        fp_filename = self.find_or_retrieve_fp(ofp)
        if fp_filename:
            self.request_id = request_id
            self.fp_filename = fp_filename

            # delete old XML file from the FMS plans folder
            self.delete_old_xml_files()

            data = self.parse_ofp(ofp)
            if self.create_xml_file(data, fp_filename):
                self.message = f"All set!"
                # get more info
                weights = ofp.get('weights')
                u = ofp.get('params').get('units')
                self.fp_info = {
                    'co route': fp_filename,
                    'oew': f"{weights.get('oew')} {u}",
                    'pax': f"{weights.get('pax_count_actual')}",
                    'cargo': f"{weights.get('cargo')} {u}",
                    'payload': f"{weights.get('payload')} {u}",
                    'zfw': f"{weights.get('est_zfw')} {u}"
                }

    def parse_ofp(self, ofp: json) -> dict:
        """
        LIDO: \n400 288/021 -54  400 320/020 -54  400 332/028 -55  350 330/022 -44\n380 272/019 -50  380 310/016 -50  380 333/024 -51  310 343/025 -34\n360 285/017 -46  360 319/017 -46  360 331/023 -46  200 005/009 -10\n340 301/016 -42  340 326/019 -42  340 328/021 -42  150 297/004 +02\n320 313/019 -36  320 328/021 -37  320 332/024 -37  100 258/001 +11
        """
        layout = ofp.get('params').get('ofp_layout')
        fix = ofp.get('navlog').get('fix')[-1]
        if fix.get('ident') == ofp.get('destination').get('icao_code'):
            dest_isa = int(fix.get('oat_isa_dev'))
        else:
            # use avg isa dev
            dest_isa = int(ofp.get('general').get('avg_temp_dev'))
        dest_metar = ofp.get('destination').get('metar')

        return {
            'dest_isa': dest_isa,
            'dest_metar': dest_metar,
            'winds': extract_descent_winds(ofp, layout=layout)
        }

    def find_or_retrieve_fp(self, ofp: dict) -> str | bool:
        recent = datetime.now() - timedelta(days=DAYS)
        self.origin = ofp.get('origin').get('icao_code')
        self.destination = ofp.get('destination').get('icao_code')
        if not (self.origin and self.destination):
            return False
        files = [
            f for f in self.path.iterdir()
            if f.suffix in ('.fms', '.fmx')
            and datetime.fromtimestamp(f.stat().st_ctime) > recent
            and f.stem.startswith(self.origin)
            and self.destination in f.stem
        ]
        if files:
            # user already created a FP for this OFP
            file = max(files, key=lambda x: x.stat().st_ctime)
            return file.stem
        else:
            # we need to download FP from SimBrief
            self.fp_link = ofp.get('fms_downloads').get('directory') + ofp.get('fms_downloads').get('xpe').get('link')
            file = Path(self.path, self.origin + self.destination + '.fms')
            result = self.download(self.fp_link, file)
            if result:
                return result.stem
        return False

    def create_xml_file(self, data: dict, fp_filename: str) -> bool:
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

        root = ET.Element("OFP")
        text = ET.SubElement(root, "text")
        plan_html = ET.SubElement(text, "plan_html")
        plan_html.text = summary_tag + dest_isa + wind_tag + winds + wx_tag + dest_metar

        filename = fp_filename + '.xml'
        file = Path(self.path, filename)
        tree = ET.ElementTree(root)
        try:
            tree.write(file, encoding='utf-8', xml_declaration=True)
            return True
        except Exception as e:
            self.message = "Error writing xml file"
            self.error = e
            return False

    def delete_old_xml_files(self):
        for f in self.path.iterdir():
            if f.suffix == '.xml':
                f.unlink()

def extract_descent_winds(ofp: dict, layout: str) -> list:
    """
    Descent wind have to be extracted from plan_html section, so it's dependant on OFP layout
    """
    if any(s in layout for s in ('RYR', 'LIDO', 'THY', 'ACA')):
        text = ofp.get('text').get('plan_html').split('DESCENT')[1].split('\n\n')[0]
        lines = text.split('\n')[1:]
        return [tuple(l.split()[-3:]) for l in lines]
    elif layout == 'UAL 2018':
        text = ofp.get('text').get('plan_html').split('DESCENT WINDS')[1].split('STARTFWZPAD')[0]
        lines = text.split('</tr><tr>')[1:5]
        winds = []
        for l in lines:
            table = ET.XML(f"<html> + {l} + </html>")
            rows = iter(table)
            winds.append(tuple(row.text.strip().replace('FL', '') or '+15' for row in rows))
    elif layout == 'DAL':
        text = ofp.get('text').get('plan_html').split('DESCENT FORECAST WINDS')[1].split('*')[0]
        lines = text.split('\n')[1:-1]
        data = list(zip(*[line.split() for line in lines]))
        idx100 = list(map(lambda x:x[0], data)).index("10000") + 1
        return [(el[0][:-2], f"{el[1][:2]}0/{el[1][-3:]}", '+15') for el in data][:idx100]
    elif layout == 'SWA':
        text = ofp.get('text').get('plan_html').split('DESCENT WINDS')[1].split('\n\n')[0]
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
        text = ofp.get('text').get('plan_html').split('CRZ ALT')[1].split('DEFRTE')[0]
        lines = lines = text.replace('FL', '').split('\n')[:3]
        return [(*l.split()[-2:], '+15') for l in lines]
    else:
        # AAL, QFA have no descent winds in OFP
        # AFR, DLH, UAE, JZA, JBU, GWI, EZY, ETD, EIN, BER, BAW, AWE have no 738 or are not operative
        return [('', '', '')]*5
    return winds


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
        self.request_id = None  # OFP generated ID
        self.fp_info = {}  # information to display in the settings window

        # status flags
        self.flight_started = False  # tracks simulation phase
        self.fp_checked = False  # tracks app phase

        # load settings
        self.load_settings()

        # widget
        self.settings_widget = None
        self.fp_info_caption = []
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

    def create_main_menu(self):
        # create Menu
        menu = xp.createMenu('SimBrief2Zibo', handler=self.main_menu_callback)
        # add Menu Items
        xp.appendMenuItem(menu, 'Settings', 1)
        return menu

    def main_menu_callback(self, menuRef, menuItem):
        """Main menu Callback"""
        if menuItem == 1:
            if not self.settings_widget:
                self.create_settings_widget(100, 400)
            elif not xp.isWidgetVisible(self.settings_widget):
                xp.showWidget(self.settings_widget)

    def create_settings_widget(self, x: int = 100, y: int = 400):

        left, top, right, bottom = x + MARGIN, y - HEADER - MARGIN, x + WIDTH - MARGIN, y - HEIGHT + MARGIN

        # main windows
        self.settings_widget = xp.createWidget(x, y, x+WIDTH, y-HEIGHT, 1, f"SimBrief2Zibo {__VERSION__}", 1, 0, xp.WidgetClass_MainWindow)
        xp.setWidgetProperty(self.settings_widget, xp.Property_MainWindowHasCloseBoxes, 1)
        xp.setWidgetProperty(self.settings_widget, xp.Property_MainWindowType, xp.MainWindowStyle_Translucent)

        # PilotID sub window
        self.pilot_id_widget = xp.createWidget(left, top, right, top - LINE - MARGIN*2, 1, "", 0, self.settings_widget, xp.WidgetClass_SubWindow)

        l, t, r, b = left + MARGIN, top - MARGIN, right - MARGIN, top - MARGIN - LINE
        caption = xp.createWidget(l, t, l + 90, b, 1, 'Simbrief PilotID:', 0,
                                  self.settings_widget, xp.WidgetClass_Caption)
        self.pilot_id_input = xp.createWidget(l + 90, t, l + 147, b, 1, "", 0,
                                              self.settings_widget, xp.WidgetClass_TextField)
        xp.setWidgetProperty(self.pilot_id_input, xp.Property_MaxCharacters, 10)
        self.pilot_id_caption = xp.createWidget(l + 90, t, l + 147, b, 1, "", 0,
                                                self.settings_widget, xp.WidgetClass_Caption)
        self.save_button = xp.createWidget(l + 150, t, r, b, 1, "SAVE", 0,
                                           self.settings_widget, xp.WidgetClass_Button)
        self.edit_button = xp.createWidget(l + 150, t, r, b, 1, "CHANGE", 0,
                                           self.settings_widget, xp.WidgetClass_Button)

        t = b - MARGIN*2
        # info message line
        self.info_line = xp.createWidget(left, t, right, t - LINE, 1, "", 0,
                                         self.settings_widget, xp.WidgetClass_Caption)
        xp.setWidgetProperty(self.info_line, xp.Property_CaptionLit, 1)

        t -= LINE + MARGIN
        # reload OFP button
        self.reload_button = xp.createWidget(l + 150, t, r, t - LINE, 0, "RELOAD", 0,
                                             self.settings_widget, xp.WidgetClass_Button)

        t -= LINE + MARGIN
        # OFP info sub window
        self.fp_info_widget = xp.createWidget(left, t, right, bottom, 1, "", 0, self.settings_widget, xp.WidgetClass_SubWindow)
        xp.setWidgetProperty(self.fp_info_widget, xp.Property_SubWindowType, xp.SubWindowStyle_SubWindow)
        t -= MARGIN
        b = bottom + MARGIN
        w = r - l
        cap = xp.createWidget(l, t, r, t - LINE, 1, 'OFP INFO:', 0,
                                  self.settings_widget, xp.WidgetClass_Caption)
        self.fp_info_caption.append(cap)
        t -= LINE + MARGIN
        while t > b:
            cap = xp.createWidget(l, t, r, t - LINE, 1, '--', 0,
                                  self.settings_widget, xp.WidgetClass_Caption)
            self.fp_info_caption.append(cap)
            t -= LINE

        self.setup_widget()

        # Register our widget handler
        self.widgetHandlerCB = self.widgetHandler
        xp.addWidgetCallback(self.settings_widget, self.widgetHandlerCB)
        xp.setKeyboardFocus(self.pilot_id_input)

    def widgetHandler(self, inMessage, inWidget, inParam1, inParam2):
        if xp.getWidgetDescriptor(self.info_line) != self.message:
            xp.setWidgetDescriptor(self.info_line, self.message)

        if self.zibo_loaded and self.fp_checked and self.fp_info:
            if not any(self.fp_info.get('zfw') in xp.getWidgetDescriptor(el) for el in self.fp_info_caption):
                self.populate_info_widget()
            if not xp.isWidgetVisible(self.fp_info_widget):
                for line in self.fp_info_caption:
                    xp.showWidget(line)
                xp.showWidget(self.fp_info_widget)
        else:
            xp.hideWidget(self.fp_info_widget)
            for line in self.fp_info_caption:
                xp.hideWidget(line)

        if self.fp_checked and not self.flight_started:
            xp.showWidget(self.reload_button)
        else:
            xp.hideWidget(self.reload_button)

        if inMessage == xp.Message_CloseButtonPushed:
            if self.settings_widget:
                xp.hideWidget(self.settings_widget)
                return 1

        if inMessage == xp.Msg_PushButtonPressed:
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

    def populate_info_widget(self):
        for i, (k, v) in enumerate(self.fp_info.items(), 1):
            xp.setWidgetDescriptor(self.fp_info_caption[i], f"{k.upper()}: {v}")

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
                    self.loop_schedule = DEFAULT_SCHEDULE
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
        xp.destroyWidget(self.settings_widget)
        xp.destroyMenu(self.main_menu)
        xp.log(f"flightloop, widget, menu destroyed, exiting ...")
