# Allsky lens calibration

Telescope markers are drawn on the allsky image by projecting each rig's pointing (alt/az)
through a fisheye model of the allsky camera, `observatory.lens` in config.json:

```
z = zenith angle (rad), a = azimuth (rad, N = 0, E = 90) + rot_deg
r = k1·z + k2·z³ + k3·z⁵           pixels from the centre
x = cx − r·sin(a)                  east is left (the sky seen from below)
y = cy − r·cos(a)                  north is up
```

`python -m cloudroll lensfit` fits that model from one clear night frame by matching the
stars in it to the Hipparcos catalogue.

## Running it

The fit needs scipy and skyfield, which the service doesn't, so install them separately
(into the same venv or another one):

```sh
.venv/bin/pip install -r requirements-lensfit.txt
.venv/bin/python -m cloudroll lensfit 20261016T063900Z
```

`FRAME` is an image file or the UTC stamp of a stored allsky frame
(`data/sources/<date>/<source>/<stamp>.jpg`, kept 7 days). Pick a clear, moonless frame
with a high star count; the Calibration page's star counts help.

The stars' positions are computed for the frame's **mid-exposure** time. For a stored
frame this comes from its capture record: indi-allsky's `image_time` (the exposure start)
plus half the exposure. Records from before star counts were recorded (2026-10-16) don't
have it, so the capture stamp is used, which is typically 0–20 s late (up to
≈ 0.1° of sky rotation). `--time 2026-10-12T08:37:50Z` overrides it.

Output, in `data/lensfit/`:

* `<stamp>.lens.json` – the model, plus how it was fitted (frame, time, stars matched,
  RMS error overall and by altitude band).
* `<stamp>.check.jpg` – red crosses are catalogue stars where the model puts them, green
  rings are the stars it matched, blue lines are the model's 0/30/60° altitude rings and
  N/E/S/W spokes. Red crosses with no green ring are normal near the horizon and in thin
  cloud; red crosses that sit beside their stars mean a bad fit.

It prints the change from the current `observatory.lens` and the snippet to paste into
config.json. It never edits config.json itself.

Options: `--anchor HIP:x,y` (repeatable) seeds the fit from stars you have identified by
hand, for when the automatic search fails (a different camera, a badly off-centre lens);
`--maglim` (default 5.5) is the faintest catalogue star used; `--pressure` (hPa, default
900) sets refraction; `--lat`/`--lon` override the site (default `lens.lat`/`lon`, else
the location).

## How it works

1. **Detect stars**: subtract a median-filtered background, threshold at 6× the noise,
   keep blobs of 3–400 px inside 48 % of the image size, brightest first (the top 1500).
2. **Catalogue**: Hipparcos stars brighter than `maglim`, at the mid-exposure time, with
   proper motion and refraction, above 3° altitude. The catalogue (`hip_main.dat`, 53 MB)
   and the ephemeris (`de421.bsp`, 17 MB) are downloaded into `data/skydata/` the first
   time.
3. **Seed**: a coarse grid over centre (±2 % of the image), rotation (±20°) and scale,
   scored by how many stars brighter than magnitude 3 land within 25 px of a detection.
   Ranges scale with the image, but rotation is limited to ±20°: a camera mounted further
   from north needs `--anchor`.
4. **Refine**: least squares (soft-L1, robust to mismatches), growing outward from the
   zenith (45° → 85° zenith angle) with tightening match radii (20 → 8 px), so the edge
   is never extrapolated blind. Final RMS uses a 6 px match radius.
5. **Check**: a clear night here matches about 55 % of the catalogue. A fit matching fewer
   than 15 % (at least 100 stars) is refused: cloud, twilight or a wrong seed still match
   a few dozen stars by chance, and those can have a deceptively low RMS (a daytime frame
   gave 34 stars at 3.9 px).

## Current calibration (October 2026)

ASI676MC (3552 × 3552, 2.0 µm pixels) with a ZWO 2.5 mm 170° lens, under indi-allsky.

| Fit | Stars | RMS | Centre | Rotation | k1 / k2 / k3 |
|-----|-------|-----|--------|----------|--------------|
| 2026-10-12 02:37:50 MDT, prototype (seeded from Capella, Vega, Betelgeuse, Aldebaran) | 646 | 3.1 px (0.14°) | 1723.4, 1843.2 | 5.52° | 1268.5 / −67.5 / 0.44 |
| same frame, `lensfit`, no anchors | 646 | 3.1 px (0.14°) | identical | identical | identical |
| 2026-10-16 00:39 MDT, `lensfit` | 737 | 3.3 px (0.15°) | 1723.0, 1841.7 | 5.52° | 1266.8 / −60.6 / −4.6 |

The two nights agree to 1.5 px, so the camera hasn't moved. Error by altitude
(2026-10-16): 0.14° above 45°, 0.15° at 20–45°, 0.21° below 20°. The prototype's notes
thought it was about 0.6° below 20°; with more low stars matched it is much better than
that. Effective focal length 2.54 mm; the image circle reaches about 7° altitude.

The original frame is kept outside git, in `data/lensfit/20261012T083800Z.jpg`, as a
regression check: `lensfit` on it with `--time 2026-10-12T08:37:50Z` should reproduce the
first row.

## indi-allsky's own lens solver

indi-allsky (v2026.08 here) can solve its lens geometry itself: **VirtualSky → Solve**
fits six values (azimuth angle, latitude/longitude offset, which model camera tilt, image
circle diameter, X/Y offset) from the displayed frame, and **Save** writes `LENS_AZIMUTH`
and the VirtualSky settings to the camera config. Azimuth also positions the N/E/S/W
labels burned into its images. The values are shown on the public `/virtualsky` page, so
CloudRoll could read them with a GET and no login.

Checked 2026-10-16: on this camera it hasn't been solved yet (azimuth 0, offsets 0,
diameter 3500), and the burned-in **N** sits about 5° from true north as a result.

Its model is VirtualSky's **equisolid** fisheye, r ∝ sin(z/2). The best equisolid curve
differs from this lens's fitted curve by up to 23 px (**about 1°**, RMS 8.6 px),
against 0.15° for `lensfit`. So indi-allsky's geometry is good as a seed or a sanity
check, but not a replacement for the fit. Its tilt terms are something CloudRoll's model
lacks.

If indi-allsky's geometry is solved and saved, its values relate to CloudRoll's as
(from the VirtualSky page's overlay code; check the signs against a solved camera):

* `cx = width/2 + OFFSET_X`, `cy = height/2 − OFFSET_Y`
* horizon radius = `IMAGE_CIRCLE_DIAMETER / 2` (r at z = 90° under equisolid; this lens's
  fit gives 1735 px, i.e. a diameter of about 3470–3530)
* `rot_deg` ≈ `LENS_AZIMUTH` (sign to be confirmed)

## Generalising: next steps

* **Pick the frame automatically**: choose the darkest, most star-filled moonless minute
  from the calibration data (star count, moon altitude), so a re-fit is one command or a
  monthly job, and warn when the model drifts.
* **Camera tilt**: add a tilt (two angles) to the model. It should help near the horizon
  and would map directly onto indi-allsky's latitude/longitude offsets.
* **Plate solving instead of the coarse search**: a blind solver (tetra3, or
  astrometry.net on a crop near the zenith) would find the rotation for any mounting and
  remove the ±20° limit and the need for `--anchor`.
* **Crop-aware sources**: a source `crop` changes the pixel coordinates; the model should
  be fitted on, or converted to, the frame as stored.
* **Read indi-allsky's solved geometry** (above) as a seed and a cross-check.
* **Non-indi-allsky cameras**: everything except the `image_time` lookup is generic;
  other cameras need `--time` or their own timestamp source.
