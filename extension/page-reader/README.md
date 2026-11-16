# Cecil Page Reader (Chrome extension)

Lets Cecil read the page you are looking at in your own Chrome, so he can guide you through a site
(e.g. the Twilio console). **Nothing is read until you click** the toolbar button or press
**Alt+Shift+C**; the extension uses `activeTab`, so it has no access to any site otherwise.

## Install (once)

1. Open `chrome://extensions` and turn on **Developer mode** (top right).
2. **Load unpacked** -> pick this folder (`<ClipAgent app>/extension/page-reader`).
3. Pin "Cecil Page Reader" from the puzzle-piece menu.
4. Optional: right-click it -> **Options** -> **Test connection**. The default address is
   `http://127.0.0.1:8000`. Enter the dashboard token only if the dashboard asks for one on this laptop.

## What it sends

The tab's URL, title, selected text, headings, visible text (all frames it can reach) and the labels of
buttons, links and fields, to `POST /pagereader/capture` on ClipAgent (this laptop only). It never sends
what is typed into fields and skips password fields; the server also masks secret-looking values
(Twilio auth tokens, API keys). Only the latest capture is kept; a new click replaces it.
`python -m assistant.page_reader clear` deletes it.

The badge shows `OK` when Cecil has the page, `ERR` if ClipAgent is not running or the page cannot be
read (Chrome's own pages and the Web Store block every extension).
