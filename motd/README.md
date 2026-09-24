# Custom MOTD - Make Your Login Banner Yours

Every time I SSH into a box I get the same wall of Ubuntu boilerplate: a welcome
line, three documentation links, an ESM ad, and a count of updates I am not going
to apply right now. I'd rather see which machine I just landed on, in big letters,
plus the one reminder that actually matters for that box.

```
garylvov@thunder:~$ ssh minerva
Welcome to Ubuntu 24.04.4 LTS (GNU/Linux 7.0.0-31-generic x86_64)

 * Documentation:  https://help.ubuntu.com
 ...
113 updates can be applied immediately.
```

## Where that text comes from

Two places, concatenated at login by `pam_motd`:

1. **`/etc/update-motd.d/`** - executable scripts, run in filename order, stdout
   concatenated. `00-header` is the welcome line, `10-help-text` the links,
   `50-motd-news` the fetched blurbs, `88-esm-announce` and
   `91-contract-ua-esm-status` the ESM nags, `90-updates-available` the counts.
2. **`/etc/motd`** - a static file, printed after the scripts.

The generated output is cached at `/run/motd.dynamic` and regenerated per login,
so there is nothing to refresh by hand.

## Making a banner

[patorjk's Text to ASCII Art Generator](https://patorjk.com/software/taag/) has a
pile of TheDraw fonts that are *in color* - shaded 3D block letters, not just
plain ASCII. The catch is that TAAG is a client-side app, so you cannot curl the
page and get your art out of it, and copy-pasting from the browser drops the
color.

`taag2ansi.py` does it properly. Give it the url from TAAG's address bar:

```
./taag2ansi.py 'https://patorjk.com/software/taag/#p=display&f=TimeSpiralG&t=Minerva&x=none&v=4&h=4&w=80&we=false&ft=thedraw' -o minerva.ans
```

or skip the url and name the font directly:

```
./taag2ansi.py --font DarkCS3DRed --text GPU -o gpu.ans
```

Then just `cat minerva.ans` to see it. Only TheDraw fonts (`ft=thedraw` in the
url) carry color - plain FIGlet fonts have none to preserve, and the script tells
you so instead of guessing. Note that TAAG's filters (`x=rainbow1` and friends)
apply to FIGlet text output, not to color fonts, which bring their own palette.

Stdlib only, no pip install, no pixi env - it runs on the `python3` that ships
with Ubuntu.

## Colors

By default the art is emitted as 24-bit color over a transparent background, so
it looks like the website does: the greens come from CGA's real RGB values rather
than whatever your terminal theme calls "bright green", and the letters sit on
your terminal's own background instead of a black slab. Nothing to pass - just
render.

See the color slots, as your terminal renders them and as RGB, with:

```
./taag2ansi.py --show-palette
```

To recolor, override slots from 0 with `--palette`. Slot 2 is green and slot 10 is
bright green, so a more acid green banner is:

```
./taag2ansi.py --font TimeSpiralG --text Minerva \
  --palette '#000000,#0000AA,#00CC22,#00AAAA,#AA0000,#AA00AA,#AA5500,#AAAAAA,#555555,#5555FF,#66FF77' \
  -o minerva.ans
```

You only need to list up to the slot you care about; the rest keep CGA values.
Only slots 0-7 are reachable as backgrounds; all 16 work as foregrounds.

Two escape hatches, for a terminal that cannot do 24-bit color (check with
`echo $COLORTERM` - it should say `truecolor` or `24bit`):

- `--ansi16` falls back to the 16 ANSI color slots, letting the theme pick shades.
- `--opaque-bg` paints background slot 0 black instead of leaving it transparent.

<details>
<summary>How it gets the font (for the curious)</summary>

It follows the same chain the browser does: scrape `index-*.js` off the TAAG
page, find the `tdfFontChunkMap-*.js` it lazy-imports, look the font up in that
registry to learn which chunk holds it, then fetch the TheDraw `.tdf` binary from
`/software/taag/tdf-font-sets/`. That file is parsed directly - font entries are
delimited by `55 AA 00 FF`, each has a 94-entry glyph offset table, and color
fonts store glyphs as character/attribute byte pairs. Characters are CP437 and
attributes are CGA, so both get mapped over to Unicode and ANSI SGR. Fonts cache
in `~/.cache/patorjk` so you only pay for the download once.

Output is byte-for-byte identical to what TAAG's own renderer produces - I
checked that by pulling the site's TDF module out of its JS bundle, running it
under node, and diffing all 3054 color fonts rendering the same word. Worth
redoing if patorjk ever changes their renderer. That comparison is against
`--ansi16 --opaque-bg`, since the site emits 16-color ANSI; the default 24-bit
output is a deliberate departure.

</details>

## OSCAR in your Bash prompt session

For an orange OSCAR banner, add this line to `~/.bashrc` (adjust the checkout path):

```bash
source /oscar/data/stellex/glvov/dev_env/motd/add_to_bashrc.bash
```

It uses the bundled TimeSpiralG art, with dark orange `#CC5500` and bright
orange `#FF8C00`. Only interactive shells with terminal output print it; shell
commands and redirected output stay quiet. `NO_COLOR=1` or `TERM=dumb` prints
plain `OSCAR`. Startup needs no network, Python, or system MOTD changes.

## Installing it

```
sudo ./install.sh minerva.ans
```

That copies the banner to `/etc/motd.ans`, drops a one-line
`/etc/update-motd.d/01-banner` that cats it, and `chmod -x`'s the stock scripts
listed above. It uses `chmod -x` rather than `rm` on purpose - delete those files
and an apt upgrade will happily put them back.

Preview without logging out:

```
run-parts /etc/update-motd.d/
```

To undo:

```
sudo rm /etc/update-motd.d/01-banner /etc/motd.ans
sudo chmod +x /etc/update-motd.d/10-help-text   # and any others you want back
```

## Gotchas

- Colors are the 16-color ANSI set, so the banner picks up your terminal theme
  rather than fixed RGB. That is usually what you want.
- MOTD is only printed for interactive logins. `scp`, `sftp`, and
  `ssh host some-command` never show it, so nothing downstream breaks.
- Want it gone for just your account without touching the server config?
  `touch ~/.hushlogin`.
- `sshd_config`'s `Banner` is a *different* thing - that one prints before
  authentication. Use it for legal notices, not art.
