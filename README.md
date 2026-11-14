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
5. Fully quit iCUE from its system tray menu to prevent display flicker. Refresh your music tab and start playback.

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

NEXUS USB support uses [NexusTool by willneedit](https://github.com/willneedit/NexusTool). Its MIT license is included in [NexusTool-LICENSE.txt](NexusTool-LICENSE.txt).
