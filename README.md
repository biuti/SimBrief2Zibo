# SimBrief2Zibo for X-Plane 12
Fetch latest user OFP Data from SimBrief and creates the file ZIBO B737-800 modified requires to import winds info.

## Features
- Creates both _b738x.xml_ and _b738x.fms_ files needed for Zibo ver.5.3+ UPLINK feature from latest SimBrief OFP
- To use standard COROUTE option, if a recent flightplan file for the flight is not available, it copies the created fms file with the name reported in plugin widget

## How to use
It works only with Zibo B737-800 modified. It requires user SimBrief Pilot ID to work. Just save it in the plugin settings.
> [!NOTE]
> Pilot ID is a number, you can find it in your SimBrief Account Settings.
> Not your username, nor your password

When Zibo aircraft is selected, **at the gate with engines off**, the plugin will start to look for latest OFP on SimBrief.
It will then create the files needed for the UPLINK function, and look for a suitable flightplan for the COROUTE function. 
You don't need to move or delete any file, the plugin manages them on its own.
Checks for fms or fmx files with _originICAO_ and _destinationICAO_ in filename. If it doesn't find one (created in the last 48 hours), it copies the fms file with name _OriginICAODestinationICAO_

If you start X-Plane before creating the OFP in SimBrief, or you need to change it, you can still do so **as long as you are on the ground with both engines off**. Create the new OFP and then click the **RELOAD** button on the plugin widget. The process could take up to 20 seconds.

Once the engines are started, the plugin will stop looking for a plugin until on the ground and with engines cutoff again, then will look for a new turnaround OFP.

**Turnaround flight**: as soon as you are at the gate with both engines off, the plugin will delete the old OFP info in the widget and will start looking for a new one in SimBrief.
During the flight the plugin goes in a standby mode to not interfere with the flight (it wouldn't anyway as it is really light, but anyway) so it could take up to a couple of minutes from when you shut down engines at the gate. It will look then on SimBrief until it detects a NEW OFP that you will create meanwhile.

### Recognized OFP Layouts
I implemented only LIDO and the airlines which have B738 in service today or recently:
- ACA
- DAL
- KLM
- RYR
- SWA
- THY
- UAL 2018

AAL and QFA apparently have no descent wind info in their OFP.


## Requirements
- MacOS 10.14, Windows 7 and Linux kernel 4.0 and above
- X-Plane 12.07 and above (not tested with previous versions, may work)
- pbuckner's [XPPython3 plugin](https://xppython3.readthedocs.io/en/latest/index.html)
- [Zibo B737-800 Modified](https://forums.x-plane.org/index.php?/forums/forum/384-zibo-b738-800-modified/) for X-Plane 12 ver.4.000.rc5.3 and above (**cannot be compatible with previous versions**)

> [!IMPORTANT]
> **You need to download correct XPPython3 version according to your Python3 installed version!
Read [instructions](https://xppython3.readthedocs.io/en/latest/usage/installation_plugin.html) on the website**

## Installation
Just copy or move the file _PI_SimBrief2Zibo.py_ to the folder:

    X-Plane/Resources/plugins/PythonPlugins/