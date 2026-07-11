# Webcam Timeline Fetcher

Fetches images from the Cumbernauld Airport webcam history pages and builds a local timeline.

Source pages:

```text
https://www.egpgmet.net/html/webcam_1.html
https://www.egpgmet.net/html/webcam_2.html
```

The page exposes direct image URLs like:

```text
https://www.egpgmet.net/Uploads/Cam1000.jpg
https://www.egpgmet.net/Uploads/Cam2000.jpg
```

## Run Once

```powershell
python .\scripts\fetch_timeline.py --once
```

On Windows you can also double-click:

```text
run_once.bat
```

Outputs:

```text
data\images\       downloaded images
data\timeline.csv  metadata
data\timeline.html local visual timeline player
```

Open:

```text
data\timeline.html
```

The timeline page has a large image player, previous/next buttons, play/pause,
a scrubber, playback speed, a sideways thumbnail timeline, and a dark mode
toggle. It also has a date filter so playback and scrubbing can be limited to
one day once the archive spans multiple days, plus a webcam filter for switching
between Webcam 1 and Webcam 2. When `All webcams` is selected, the player shows
Webcam 1 and Webcam 2 side by side using synced timeline minutes. If one webcam
does not have an image for that minute, the player carries forward the most
recent earlier image for that webcam. If there is no earlier image, it shows a
blank fallback.

The `Flight match` toggle uses the matching flight rows near the selected
timestamp, then compares the current image with the previous image from the
same webcam and scores moving objects against the expected aircraft type. It
labels the best visual match with the callsign/type.

The `Visible aircraft` toggle loads a browser-side COCO-SSD object detector and
looks for `airplane` objects in the current image, regardless of whether the
aircraft is flying, parked, or in the flight log. The current confidence
threshold is `0.28`. If a detection lines up with
a projected flight row, the box is labelled with the callsign/type. If the model
cannot confidently identify an aircraft, the page does not draw a fallback shape
box. Both toggles are visual aids, not guaranteed aircraft classifiers.

Labelled crop examples can be stored locally in:

```text
data\aircraft_crops
```

Use tight crops around the aircraft and names like `GZOFG_P28A_001.jpg`.
Crop images are ignored by git.

You can also fetch reference images from Wikimedia Commons using the current
flight log:

```powershell
python .\scripts\fetch_aircraft_reference_images.py
```

Or search manually:

```powershell
python .\scripts\fetch_aircraft_reference_images.py --query "G-ZOFG P28A aircraft"
```

Downloaded image metadata and source/licence details are written to:

```text
data\aircraft_crops\references.csv
```

For better highlighting, add camera position/direction data to:

```text
data\cameras.csv
```

Use `data\cameras.example.csv` as the template:

```text
page_name,latitude,longitude,heading_deg,horizontal_fov_deg,vertical_fov_deg,pitch_deg,elevation_ft
```

Required fields are `page_name`, `latitude`, `longitude`, and `heading_deg`.
Use `Webcam 1` and `Webcam 2` for `page_name`. The heading is the compass
direction the camera points, where north is `0`, east is `90`, south is `180`,
and west is `270`. Field-of-view and pitch can be approximate and tuned later.

## Flight Info

The timeline page reads optional flight rows from:

```text
data\flights.csv
```

An example header is included at:

```text
data\flights.example.csv
```

Rows are matched to the selected image if their `event_time_utc` is within 10
minutes of the image timeline timestamp.

Expected columns:

```text
event_time_utc,callsign,registration,aircraft_type,origin,destination,direction,altitude_ft,groundspeed_kt,vertical_rate_fpm,track_deg,squawk,emergency,seen_seconds,on_ground,notes,source_url,hex,latitude,longitude,distance_nm,provider
```

Only `event_time_utc` is required. The other columns make the flight card more
useful.

The script now tries to populate this file automatically from free ADS-B data
sources:

1. ADSB.lol radius lookup
2. OpenSky Network bounding box fallback

The default search is intentionally tight: within 3 nautical miles of EGPG and
below 3000 ft. This avoids matching unrelated overhead traffic.

ICAO `4016D2` is highlighted in the flight cards when it appears.

Useful options:

```powershell
python .\scripts\fetch_timeline.py --once --flight-radius-nm 10
python .\scripts\fetch_timeline.py --once --flight-radius-nm 3 --flight-max-altitude-ft 2500
python .\scripts\fetch_timeline.py --once --no-flights
```

## Timestamp Handling

The camera image has its own visible timestamp, but the downloaded file also has
a server `Last-Modified` timestamp. Those do not always match.

The CSV therefore keeps both:

```text
timeline_timestamp_utc  corrected timeline time used for ordering/playback
image_timestamp_utc     raw server Last-Modified timestamp
camera_slot             Cam1000 = 0, Cam1001 = 1 minute older, etc.
page_name               Webcam 1 or Webcam 2
page_url                source page where the image was found
seen_count              number of rolling-slot observations for the image
timeline_observations_utc
                         all corrected observations used for the image
timestamp_source         rolling_slot or rolling_slot_median
```

Playback uses `timeline_timestamp_utc`, which is corrected from the camera slot
order so `Cam1000`, `Cam1001`, `Cam1002`, etc. play in the right order.
When the same image is seen again in a later rolling slot, the script records
that extra observation and uses the median corrected time. This is more robust
than trusting one server timestamp.

## Watch Every Minute

```powershell
python .\scripts\fetch_timeline.py --watch --interval 60
```

On Windows you can also double-click:

```text
watch.bat
```

The script de-duplicates images by SHA-256 hash, so fetching the same image again will not duplicate the timeline.

## Host The History From This Machine

The downloaded history lives in `data\images`, `data\timeline.csv`, and
`data\timeline.html`. Those files are intentionally not pushed to GitHub, so
host them from the machine that is collecting the images.

Start the local history server:

```powershell
python .\scripts\serve_history.py --host 0.0.0.0 --port 8000
```

On Windows you can also double-click:

```text
serve_history.bat
```

The server prints links like:

```text
Local:   http://127.0.0.1:8000/data/timeline.html
Network: http://192.168.1.50:8000/data/timeline.html
```

Use the `Local` link on this PC. Give the `Network` link to people on the same
Wi-Fi or LAN.

For people outside your home network, use a tunnel service pointed at the local
server:

```powershell
cloudflared tunnel --protocol http2 --edge-ip-version 4 --url http://127.0.0.1:8000
```

On Windows you can also double-click:

```text
start_public_tunnel.bat
```

That command prints a public `https://...trycloudflare.com` URL. Open:

```text
https://your-tunnel-url.trycloudflare.com/data/timeline.html
```

Leave both the fetcher and the history server running if you want the public
page to keep updating.

## Always-On Public Hosting

Use this launcher for Windows Task Scheduler:

```text
start_history_hosting.bat
```

It starts the local history server if needed, keeps the image/flight fetcher
running every 60 seconds, starts a Cloudflare quick tunnel, and writes the
current public URL to:

```text
data\public_url.txt
```

That file contains a `timeline_url=...` line you can copy and send.
The URL stays the same while the Cloudflare tunnel process is alive. A new URL
is only written if the tunnel has to be started again.

Recommended Task Scheduler setup:

```text
Trigger: At log on
Action:  Start a program
Program: C:\Users\kyle_\.openclaw\workspace\webcam_timeline\start_history_hosting.bat
Start in: C:\Users\kyle_\.openclaw\workspace\webcam_timeline
```

Enable these task settings:

```text
Run only when user is logged on
Run task as soon as possible after a scheduled start is missed
If the task fails, restart every 1 minute
Stop the task if it runs longer than: Disabled
```

## Custom Page Selection

By default the script fetches both webcam pages. To fetch only one page, pass
`--page-url`:

```powershell
python .\scripts\fetch_timeline.py --page-url https://www.egpgmet.net/html/webcam_2.html --once
```

You can pass `--page-url` more than once to fetch a custom set of pages.

## Scheduling

Use Windows Task Scheduler with:

```powershell
powershell -ExecutionPolicy Bypass -Command "cd C:\Users\kyle_\.openclaw\workspace\webcam_timeline; python .\scripts\fetch_timeline.py --once"
```

Run it every minute if you want a continuous timeline.
