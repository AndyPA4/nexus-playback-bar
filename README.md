# NEXUS Playback Bar

Use your Corsair iCUE NEXUS as a touchscreen seek bar for Chrome on Windows. Tap to jump or drag to scrub. The display can show titles, artists, playback progress, and cover art.

## Browser compatibility

| Website | Status in Chrome |
|---|---|
| YouTube | Tested |
| Amazon Music | Tested |
| Spotify Web Player | Tested |
| Apple Music on the web | Tested |
| SoundCloud | Tested |
| Prime Video | Title and artwork vary by page |
| Other web players | Detection, artwork, and seeking depend on the player |

## Install v0.2.31

1. Connect your NEXUS over USB. Install Python 3 for Windows with Tkinter and the Python launcher (`py.exe`).
2. Download `nexus-playback-bar-v0.2.31.zip` from [Releases](../../releases) and extract it to a folder you can keep.
3. Run `Install-NEXUS.cmd`, then open **NEXUS Playback Bar** from Start.
4. In `chrome://extensions`, enable **Developer mode**, click **Load unpacked**, and select the extracted `chrome` folder. Pin the NPB icon from Chrome’s Extensions menu for easy access.
5. If iCUE is running, follow the [iCUE workaround](#use-nexus-playback-bar-while-icue-is-running) below or fully quit iCUE from the system tray to prevent display flicker. Refresh your media tab and start playback.

## Use NEXUS Playback Bar while iCUE is running

This workaround was tested on **iCUE 5.52.93**. It lets iCUE manage your other Corsair devices while NEXUS Playback Bar controls the NEXUS screen.

1. Unplug the NEXUS and leave iCUE running.
2. In **iCUE → Settings → Software Updates**, disable automatic updates. If iCUE offers a removal option for the disconnected **iCUE NEXUS** module, remove that module.
3. Reconnect the NEXUS. If iCUE shows **New Device Detected** for the NEXUS, skip **Download and Install** so the module stays uninstalled.
4. Open NEXUS Playback Bar, play something in Chrome, and check that you can scrub on the NEXUS without flicker while your other Corsair devices still respond to iCUE.

With automatic updates disabled, check for iCUE updates manually. If the removal option is unavailable or the screen still flickers, fully quit iCUE from the system tray before using NEXUS Playback Bar.

## Update an existing install

1. Right-click the NPB tray icon and choose **Quit**.
2. Extract the new ZIP and run its `Install-NEXUS.cmd` again. Your saved settings carry over.
3. Replace the files in the existing unpacked extension folder with the contents of the new `chrome` folder, including `icons`. Go to `chrome://extensions` and click **Reload** for NEXUS Playback Bar. If you load a new folder instead, disable the old copy.
4. Relaunch the Windows app from Start and refresh your media tabs.

## Tray and settings

Left-click the NPB tray icon for Settings. Right-click for **Quit**. **Start with Windows** launches the app at sign-in. **Start in tray** opens it with Settings hidden. The toggles work independently.

The extension uses site access to detect playback and sends media state to the app at `127.0.0.1`. Artwork may be fetched from the website or its image host.

## Feedback and bug reports

NEXUS Playback Bar is still in development. If you find a bug or a website that doesn’t work as expected, please [open a GitHub issue](../../issues/new) so I can look into it and fix it. Include the website or page URL, what you expected, what happened, and your app and extension versions.

## Support this project

If you’d like to leave a tip, you can [support me on Ko-fi](https://ko-fi.com/andypa4).

If NEXUS Playback Bar makes your NEXUS more useful, you can support the project by starring this repository or sharing it with someone who owns a NEXUS. Bug reports and feedback are welcome, too.

NEXUS USB support uses [NexusTool by willneedit](https://github.com/willneedit/NexusTool). Its MIT license is included in [NexusTool-LICENSE.txt](NexusTool-LICENSE.txt).
