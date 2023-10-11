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
import multiprocessing

from pathlib import Path
from urllib import request, parse
from urllib.error import URLError, HTTPError
from ssl import SSLCertVerificationError
from xml.etree import ElementTree as ET
from datetime import datetime, timedelta

try:
    from XPPython3 import xp
except ImportError:
    pass


# Version
__VERSION__ = 'v0.6.beta'

# Plugin parameters required from XPPython3
plugin_name = 'SimBrief2Zibo'
plugin_sig = 'xppython3.simbrief2zibo'
plugin_desc = 'Fetches latest OFP Data from SimBrief and creates the file ZIBO B738 requires'

# Other parameters
loop_schedule = 15  # positive numbers are seconds, 0 disabled, negative numbers are cycles
days = 2  # how recent a fp file has to be to be considered

# widget parameters
width = 250
height = 250
margin = 10
line = 12
header = 32


class PythonInterface:

    async_started = False
    async_pid = False
    response = None

    def __init__(self) -> None:
        self.plugin_name = f"{plugin_name} - {__VERSION__}"
        self.plugin_sig = plugin_sig
        self.plugin_desc = plugin_desc

        # folders init
        self.xp_root = Path(xp.getSystemPath())
        self.prefs = Path(xp.getPrefsPath()).parent
        self.plans = Path(self.prefs.parent, 'FMS plans')

        # Dref init
        self.speed = xp.findDataRef('sim/flightmodel/position/groundspeed')
        self.gears_on_ground = xp.findDataRef('sim/flightmodel2/gear/on_ground')
        self.engines_burning_fuel = xp.findDataRef('sim/flightmodel2/engines/engine_is_burning_fuel')

        xp.log(f"root: {self.xp_root} | prefs: {self.prefs} {self.prefs.is_dir()} | plans: {self.plans} {self.plans.is_dir()}")

        # app init
        self.config_file = Path(self.prefs, 'simbrief2zibo.prf')
        self.pilot_id = None  # SimBrief UserID, int
        self.request_id = None  # OFP generated ID
        self.url = None  # SimBrief API url
        self.origin = None  # OFP departure ICAO
        self.destination = None  # OFP destination ICAO
        self.fp_filename = None  # fms/fmx filename
        self.fp_link = None  # link to download XP12 fms from SimBrief
        self.fp_info = {}  # information to display in the settings window

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
    def simbrief_url(self) -> str | bool:
        return f"https://www.simbrief.com/api/xml.fetcher.php?userid={self.pilot_id}&json=1"

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
                self.create_settings_widget(200, 600)
            elif not xp.isWidgetVisible(self.settings_widget):
                xp.showWidget(self.settings_widget)

    def create_settings_widget(self, x: int = 10, y: int = 800):

        left, top, right, bottom = x + margin, y - header - margin, x + width - margin, y - height + margin

        # main windows
        self.settings_widget = xp.createWidget(x, y, x+width, y-height, 1, "Settings", 1, 0, xp.WidgetClass_MainWindow)
        xp.setWidgetProperty(self.settings_widget, xp.Property_MainWindowHasCloseBoxes, 1)

        # PilotID sub window
        self.pilot_id_widget = xp.createWidget(left, top, right, top - line - margin*2, 1, "", 0, self.settings_widget, xp.WidgetClass_SubWindow)

        l, t, r, b = left + margin, top - margin, right - margin, top - margin - line
        caption = xp.createWidget(l, t, l + 90, b, 1, 'Simbrief PilotID:', 0,
                                  self.pilot_id_widget, xp.WidgetClass_Caption)
        self.pilot_id_input = xp.createWidget(l + 90, t, l + 147, b, 1, "", 0,
                                              self.pilot_id_widget, xp.WidgetClass_TextField)
        self.pilot_id_caption = xp.createWidget(l + 90, t, l + 147, b, 1, "", 0,
                                                self.pilot_id_widget, xp.WidgetClass_Caption)
        self.save_button = xp.createWidget(l + 150, t, r, b, 1, "SAVE", 0,
                                           self.pilot_id_widget, xp.WidgetClass_Button)
        self.edit_button = xp.createWidget(l + 150, t, r, b, 1, "CHANGE", 0,
                                           self.pilot_id_widget, xp.WidgetClass_Button)
        t = b - margin*2
        # info message line
        self.info_line = xp.createWidget(left, t, right, t - line, 1, "", 0,
                                         self.settings_widget, xp.WidgetClass_Caption)
        t -= line + margin
        # OFP info sub window
        self.fp_info_widget = xp.createWidget(left, t, right, bottom, 1, "", 0, self.settings_widget, xp.WidgetClass_SubWindow)
        t -= margin
        b = bottom + margin
        w = r - l
        cap = xp.createWidget(l, t, r, t - line, 1, 'OFP INFO:', 0,
                                  self.fp_info_widget, xp.WidgetClass_Caption)
        self.fp_info_caption.append(cap)
        t -= line + margin
        while t > b:
            cap = xp.createWidget(l, t, r, t - line, 1, '--', 0,
                                  self.fp_info_widget, xp.WidgetClass_Caption)
            self.fp_info_caption.append(cap)
            t -= line
        xp.log(f"added {len(self.fp_info_caption)} info lines")

        self.setup_widget()

        # Register our widget handler
        self.widgetHandlerCB = self.widgetHandler
        xp.addWidgetCallback(self.settings_widget, self.widgetHandlerCB)
        xp.setKeyboardFocus(self.pilot_id_input)

    def widgetHandler(self, inMessage, inWidget, inParam1, inParam2):
        if xp.getWidgetDescriptor(self.info_line) != self.message:
            xp.setWidgetDescriptor(self.info_line, self.message)

        if self.fp_info:
            if not self.fp_info.get('zfw') in xp.getWidgetDescriptor(self.fp_info_caption[-1]):
                self.populate_info_widget()
            if not xp.isWidgetVisible(self.fp_info_widget):
                xp.showWidget(self.fp_info_widget)
        else:
            xp.hideWidget(self.fp_info_widget)

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
        # xp.log(f"fp_info? {bool(self.fp_info)}")
        for i, (k, v) in enumerate(self.fp_info.items(), 1):
            xp.setWidgetDescriptor(self.fp_info_caption[i], f"{k.upper()}: {v}")

    def response_callback(self, result):
        self.async_pid, self.response = result
        self.async_started = False
    def loopCallback(self, lastCall, elapsedTime, counter, refCon):
        """Loop Callback"""
        _, acf_path = xp.getNthAircraftModel(0)
        if 'B737-800X' in acf_path and self.pilot_id:
            xp.log(f"FP checked: {self.fp_checked} | At gate: {self.at_gate} | Flight started: {self.flight_started}")
            if not self.flight_started:
                if not self.fp_checked and self.at_gate:
                    # check fp
                    xp.log(f"starting FP routine...")
                    if self.response:
                        self.check_simbrief()
                    elif self.async_started:
                        if self.async_pid or datetime.now() - self.async_started > timedelta(seconds=loop_schedule*2):
                            # need to try again
                            self.async_pid = False
                            self.async_started = False
                        else:
                            # no answer yet, waiting ...
                            pass
                    else:
                        self.message = "Contacting SimBrief ..."
                        multiprocessing.set_executable(xp.pythonExecutable)
                        pool = multiprocessing.Pool()
                        self.async_pid = False
                        self.response = None
                        result = pool.apply_async(
                            get_response,
                            args=(self.simbrief_url,),
                            callback=self.response_callback
                        )
                        pool.close()
                        self.async_started = datetime.now()

                elif not self.flight_started and not self.at_gate:
                    # flight mode, do nothing
                    xp.scheduleFlightLoop(self.loop_id, loop_schedule*10)
                    self.flight_started = True
                    self.message = "Have a nice flight!"
            elif self.at_gate:
                # look for a new OFP for a turnaround flight
                xp.log(f'set flight ended...')
                xp.scheduleFlightLoop(self.loop_id, loop_schedule)
                self.flight_started = False
                self.fp_checked = False
                self.fp_info = {}
                self.message = "Looking for a new OFP ..."
        else:
            # nothing to do
            if not 'B737-800X' in acf_path:
                self.message = "Zibo not detected"
            elif not self.pilot_id:
                self.message = "SimBrief PilotID required"

        return loop_schedule

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
        user_id = int(xp.getWidgetDescriptor(self.pilot_id_input).strip())
        settings = {'settings': {'pilot_id': user_id}}
        with open(self.config_file, 'w') as f:
            json.dump(settings, f)
        # check file
        self.load_settings()
        self.setup_widget()

    def check_simbrief(self):
        xp.log(f"pilotID = {self.pilot_id}, contacting SimBrief...")
        if self.response.get('error'):
            # some error occurred
            xp.log(f"check_simbrief: {ofp.get('error')}")
            self.message = f"Error trying to connect to SimBrief"
            self.response = None
            return

        if self.request_id and self.request_id == self.response.get('params').get('request_id'):
            # no new OFP
            self.message = "No new OFP available"
            self.response = None
            return

        ofp = self.response
        self.origin = ofp.get('origin').get('icao_code')
        self.destination = ofp.get('destination').get('icao_code')
        if self.origin and self.destination:
            self.fp_link = ofp.get('fms_downloads').get('directory') + ofp.get('fms_downloads').get('xpe').get('link')
            self.fp_filename = self.get_fp_filename()
            if not self.fp_filename:
                # we need to download FP from SimBrief
                t = datetime.now()
                file = Path(self.plans, self.origin + self.destination + '.fms')
                result = download(self.fp_link, file)
                if result:
                    self.fp_filename = result
            if self.fp_filename:
                data = self.parse_ofp(ofp)
                if self.create_xml_file(data):
                    self.fp_checked = True
                    self.response = None
                    self.request_id = ofp.get('params').get('request_id')
                    self.message = f"All set: {self.fp_filename}"
                    # get more info
                    weights = ofp.get('weights')
                    u = ofp.get('params').get('units')
                    self.fp_info = {
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
        if fix.get('ident') == self.destination:
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

    def get_fp_filename(self) -> Path | None:
        recent = datetime.now() - timedelta(days=days)
        files = [
            f for f in self.plans.iterdir()
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
            return None

    def create_xml_file(self, data: dict) -> bool:
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
        filename = self.fp_filename + '.xml'
        file = Path(self.plans, filename)
        tree = ET.ElementTree(root)
        try:
            tree.write(file)
            return True
        except Exception as e:
            xp.log(f"Error writing xml file: {e}")
            return False

    def XPluginStart(self):
        return self.plugin_name, self.plugin_sig, self.plugin_desc

    def XPluginEnable(self):
        # loopCallback
        self.loop = self.loopCallback
        self.loop_id = xp.createFlightLoop(self.loop, phase=1)
        xp.scheduleFlightLoop(self.loop_id, loop_schedule)
        return 1

    def XPluginStop(self):
        # Called once by X-Plane on quit (or when plugins are exiting as part of reload)
        xp.log(f"flightloop, widget, menu destroyed, exiting ...")
        xp.destroyFlightLoop(self.loop_id)
        xp.destroyWidget(self.settings_widget)
        xp.destroyMenu(self.main_menu)


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


def get_response(url: str) -> tuple[int, dict]:
    pid = os.getpid()
    try:
        response = request.urlopen(url)
    except SSLCertVerificationError as e:
        # change link to unsecure protocol to avoid SSL error in some weird systems
        print(f" *** get_ofp() had to run in unsecure mode: {e}")
        try:
            parsed = parse.urlparse(url)
            parsed = parsed._replace(scheme=parsed.scheme.replace('https', 'http'))
            link = parse.urlunparse(parsed)
            response = request.urlopen(link)
        except (HTTPError, URLError) as e:
            print(f" *** get_ofp() unsecure mode error: {e}")
            return pid, {'error': f'Error retrieving OFP: {e}'}
    except (HTTPError, URLError) as e:
        print(f" *** get_ofp() error: {e}")
        return pid, {'error': f'Error retrieving OFP: {e}'}
    ofp = json.loads(response.read())
    return pid, ofp


def download(source: str, destination: Path) -> Path | bool:
    try:
        result = request.urlretrieve(source, destination)
    except SSLCertVerificationError:
        # change link to unsecure protocol to avoid SSL error in some weird systems
        parsed = parse.urlparse(source)
        parsed = parsed._replace(scheme=parsed.scheme.replace('https', 'http'))
        link = parse.urlunparse(parsed)
        try:
            result = request.urlretrieve(link, destination)
        except (HTTPError, URLError) as e:
            xp.log(f'Error downloading fms file: {e}')
            return False
    except (HTTPError, URLError) as e:
        xp.log(f'Error downloading fms file: {e}')
        return False
    return destination
