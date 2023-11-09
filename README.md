# SimBrief2Zibo for X-Plane 12
Fetch latest user OFP Data from SimBrief and creates the file ZIBO B737-800 modified requires for the UPLINK and CO ROUTE features.

## Features
- Creates the _b738x.xml_ file needed for Zibo ver.5.6+ UPLINK feature from latest SimBrief OFP
- To use standard CO ROUTE option, if a recent flightplan file for the flight is not available, it downloads the fms file with the name reported in plugin widget, **adding DEP and ARR procedures if available in XML file**
- **NEW** added a **D-ATIS** widget

## How to use
It works only with Zibo B737-800 modified. It requires user SimBrief Pilot ID to work. Just save it in the plugin settings.
> [!NOTE]
> Pilot ID is a number, you can find it in your SimBrief Account Settings.
> Not your username, nor your password

When Zibo aircraft is selected, **at the gate with engines off**, the plugin will start to look for latest OFP on SimBrief.

It will then create the xml file needed for the UPLINK function, and look for a suitable flightplan for the CO ROUTE function. 
You don't need to move or delete any file, the plugin manages them on its own.
Checks for fms or fmx files with _originICAO_ and _destinationICAO_ in filename. If it doesn't find one **(created in the last 48 hours)**, it downloads the fms file from SimBrief with name _OriginICAODestinationICAO_.
The plugin will also try to add departure and arrival information found in the xml file to the downloaded fms file.

If a Navigraph flight plan for is found, it will not be overwritten, so you'll have departure and arrival procedures available using the CO ROUTE function.

If you start X-Plane before creating the OFP in SimBrief, or you need to change it, you can still do so **as long as you are on the ground with both engines off**. Create the new OFP and then click the **RELOAD** button on the plugin widget. The process could take up to 20 seconds.

> [!IMPORTANT]
> **If your flight plan file for the CO ROUTE feature has been created more than 48 hours before the flight, it will be overwritten with a new one downloaded from SimBrief**

Once the engines are started, the plugin will stop looking for a plugin until on the ground and with engines cutoff again, then will look for a new turnaround OFP.

### Turnaround flights
as soon as you are at the gate with both engines off, the plugin will delete the old OFP info in the widget and will start looking for a new one in SimBrief.
During the flight the plugin goes in a standby mode to not interfere with the flight (it wouldn't anyway as it is really light, but anyway) so it could take up to a couple of minutes from when you shut down engines at the gate. It will look then on SimBrief until it detects a NEW OFP that you will create meanwhile.

### Recognized OFP Layouts
I implemented LIDO and the layouts for airlines which have B738 in service today or recently:
- ACA
- DAL
- KLM
- RYR
- SWA
- THY
- UAL 2018

AAL and QFA apparently have no descent wind info in their OFP.

### D-ATIS Widget
In this release a Digital ATIS widget was added as a new feature.
It displays latest D-ATIS for departure and destination in airports equipped with it.
Informations are grabbed from [atis.report](https://atis.report) website

## Requirements
- MacOS 10.14, Windows 7 and Linux kernel 4.0 and above
- X-Plane 12.07 and above (not tested with previous versions, may work)
- pbuckner's [XPPython3 plugin](https://xppython3.readthedocs.io/en/latest/index.html)
- [Zibo B737-800 Modified](https://forums.x-plane.org/index.php?/forums/forum/384-zibo-b738-800-modified/) for X-Plane 12 ver.4.0.**rc5.6** and above (**cannot be compatible with previous versions**)

> [!IMPORTANT]
> **You need to download correct XPPython3 version according to your Python3 installed version!
Read [instructions](https://xppython3.readthedocs.io/en/latest/usage/installation_plugin.html) on the website**

## Installation
Just copy or move the file _PI_SimBrief2Zibo.py_ to the folder:

    X-Plane/Resources/plugins/PythonPlugins/