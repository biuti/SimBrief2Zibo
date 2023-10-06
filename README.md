# SimBrief2Zibo for X-Plane 12
Fetch latest user OFP Data from SimBrief and creates the file ZIBO B737-800 modified requires to import winds info.

## Features
- Creates the XML file from latest SimBrief OFP
- If a recent flightplan file for the flight is not available, it downloads the fms file for the OFP flight from SimBrief

## How to use
It works only with Zibo B737-800 modified. It requires user SimBrief PilotID to work. Just save it in the plugin settings.
When Zibo aircraft is selected, at the gate with engines off, the plugin will start to look for latest OFP on SimBrief.
It will then look for a suitable flightplan. Checks for fms or fmx files with _originICAO_ and _destinationICAO_ in filename. If it finds one (created in the last 48 hours), uses the same filename for the xml file. Otherwise, it will download from SimBrief the fms file for the OFP and will name both fms and xml files _OriginICAODestinationICAO_
Once the engines are started, the plugin will stop looking for a plugin until on the ground and engines cutoff again, then will look for a new turnaround OFP.

## Requirements
- MacOS 10.14, Windows 7 and Linux kernel 4.0 and above
- X-Plane 12.07 and above (not tested with previous versions, may work)
- pbuckner's [XPPython3 plugin](https://xppython3.readthedocs.io/en/latest/index.html)
- [Zibo B737-800 Modified](https://forums.x-plane.org/index.php?/forums/forum/384-zibo-b738-800-modified/) for X-Plane 12 ver.4.000.rc4.0 and above (not tested with previous versions, may work)

> [!IMPORTANT]
> **You need to download correct XPPython3 version according to your Python3 installed version!
Read [instructions](https://xppython3.readthedocs.io/en/latest/usage/installation_plugin.html) on the website**

## Installation
Just copy or move the file _PI_SimBrief2Zibo.py_ to the folder:


    X-Plane/Resources/plugins/PythonPlugins/