# Cecil Page Reader (Chrome extension)

Keeps Cecil up to date with the page you are on in your own Chrome, and lets him click, type and scroll
there when you ask him to (e.g. "walk me through the Twilio console", "click Save for me").

## Install / update

1. Open `chrome://extensions` and turn on **Developer mode** (top right).
2. **Load unpacked** -> pick this folder (`<ClipAgent app>/extension/page-reader`). After an update,
   press the **reload** arrow on the extension's card instead (v2 asks for access to all sites once).
3. Pin "Cecil Page Reader" from the puzzle-piece menu.
4. Optional: right-click it -> **Options** -> **Test connection**. The default address is
   `http://127.0.0.1:8000`. Enter the dashboard token only if the dashboard asks for one on this laptop.

## What it does

- **Reads automatically**: when you switch tab or window, when a page finishes loading, and every 30 s
  (sent only if the page changed). The toolbar button or **Alt+Shift+C** reads right now.
- **Acts for Cecil**: it checks ClipAgent for one action at a time (click, type, press Enter, scroll, open
  an address, go back) and performs it in the active tab, then sends the result and a fresh read.

## Options

- **Read pages automatically** - off = only the button / shortcut reads a page.
- **Let Cecil click, type and scroll** - off = he can only read.
- **Never read these sites** - e.g. your bank; neither reading nor acting ever happens there.

## What it sends

The tab's URL, title, selected text, headings, visible text (all frames it can reach) and the labels of
buttons, links and fields, to ClipAgent on this laptop only. It never sends what is typed into fields,
skips password fields and never types into them; the server also masks secret-looking values. Only the
latest read is kept; `python -m assistant.page_reader clear` deletes it.

The badge shows `OK` when Cecil has the page or an action worked, `ERR` if ClipAgent is not running or
the page cannot be touched (Chrome's own pages and the Web Store block every extension), `OFF` on a
site from the Never read list.
