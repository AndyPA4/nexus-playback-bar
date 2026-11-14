# NEXUS Playback Bar

Use your Corsair iCUE NEXUS as a touchscreen seek bar for Chrome. I built this so I could see what was playing and jump to a different spot without leaving the browser tab.

**Latest build** · v0.2.25 · Windows + Google Chrome

## What it does

- Shows the media title, elapsed time, total duration, and channel or artist when available.
- Lets you tap to seek or drag to scrub on the NEXUS or in the Windows preview. By default, the video seeks when you lift your finger. Live seeking is optional.
- Uses the current thumbnail for the background and seek bar color when artwork is available. You can also use a solid color or your own PNG or GIF background.
- Lets you adjust text layout, title alignment, brightness, outlines, and seek bar thickness. Long titles scroll in the inline layouts.
- Runs from the Windows system tray and saves your settings between sessions.

## Setup

You’ll need a Corsair iCUE NEXUS connected over USB, a Windows PC with Python 3 and Tkinter, and Google Chrome.

1. Download **nexus-playback-bar-v0.2.25.zip** from the repository’s **Releases** tab and extract it. Keep the files together.
2. Open `chrome://extensions`, enable **Developer mode**, click **Load unpacked**, and select the extracted `chrome` folder.
3. Fully quit iCUE from its system tray menu. Both apps can draw to the NEXUS at the same time, which causes flicker.
4. Run `nexus-playback.pyw`. Refresh your media tab and play a YouTube video to check that the seek bar appears.

To update, close the running app and extract the new ZIP over the existing files. Click **Reload** on the extension in `chrome://extensions`, refresh your media tab, then launch the updated Windows app.

## Tray and settings

Close the Settings window or click **Hide** to keep the app running in the tray. Left-click the tray icon once to reopen Settings. Right-click the icon and choose **Quit** to stop the app. Launch the app again after signing in to Windows.

Settings save automatically to `%LOCALAPPDATA%\NexusPlaybackBar\settings.json`. To reset them, quit the app and delete that file.

## Browser support

YouTube is the most tested site. The extension also detects compatible web players, including accessible embedded players. Prime Video and Amazon Music support varies by page. Some players limit seeking or provide incomplete titles and artwork.

The extension needs site access for automatic detection. It sends playback position and media metadata to the Windows app over a local connection at `127.0.0.1`. Artwork may be downloaded from the media site or its image host.

If a site behaves differently, open an issue with the site name, the app and extension versions, and what you see on the NEXUS.

## Credits

The NEXUS USB code uses [NexusTool by willneedit](https://github.com/willneedit/NexusTool). Its MIT license is included in [NexusTool-LICENSE.txt](NexusTool-LICENSE.txt).
