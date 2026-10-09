# dataset_to_seg.py

Converts a real-world dataset into a valid `.seg` model, so that

```
dataset  --(mapping.yaml)-->  .seg  -->  generate_twin.py  -->  digital twin
```

The mapping file is where the user says what the dataset *is* (which column is a
load, a voltage, an irradiance, and which node class it becomes). Everything else
is computed from the data.

## Usage

```bash
# from the project root (where generate_twin.py lives)
python tools/dataset_to_seg.py \
    --mapping tools/examples/entsoe_load_gr.map.yaml \
    --out entsoe_load_gr.seg

# also run generate_twin.py on the result (in a temp dir) as an end-to-end check
python tools/dataset_to_seg.py --mapping tools/examples/nasa_power_solar.map.yaml \
    --out nasa_solar.seg --check-twin generate_twin.py --templates templates

python tools/dataset_to_seg.py --self-test     # synthetic recovery tests
```

Requires: `numpy pandas scipy pyyaml textx jinja2` (+ `duckdb` for the DuckDB loader).

Data files are expected in `data/` (paths in the mappings are relative to the
mapping file). 
## What it does

1. **Load** (DuckDB query, CSV, NASA POWER CSV, ENTSO-E XML).
2. **Node attributes**: literals from the mapping or statistics derived from the data.
   Every attribute in the generated `.seg` carries a comment saying which.
3. **Noise variables**: for each variable in the mapping the noise component is
   extracted (detrending), the DSL's parametric families are fitted by MLE
   (`NORMAL`, `LOGNORMAL`, `GAMMA`, `WEIBULL`, `BETA`, whichever are admissible for
   the data's support), and the fit is validated on a chronological **hold-out**.
4. **Write the `.seg`** and check it with the real textX grammar. A file that does not
   parse is never left behind.

Units follow the simulator (`VARIABLE_SEMANTICS` in the script cites the template
lines): `voltage_noise` and `load_noise` are *relative* residuals, `solar_noise` is
*additive on the cloud fraction*, `wind_speed` is fitted on the raw values.

## Validation rule (fixed in advance)

* Among the detrending windows in the mapping, take the **smallest** whose best
  candidate passes the KS test (alpha = 0.05) on the **train** part (first 70 %).
* Freeze the parameters and test them on the **hold-out** (last 30 %).
* `ACCEPTED` needs both. Otherwise the variable is **not written**: the system
  default stays and the `.seg` carries a comment with the reason.
  `--allow-rejected` writes the best-AIC fit anyway, labelled `WARNING`.
* The KS test runs on a sample thinned to the effective size
  `n(1-rho1)/(1+rho1)` (rho1 = lag-1 autocorrelation of the residuals).

## Limitations (state them in the thesis)

* **Only parametric distributions** (grammar limitation, section 11.2): the twin
  reproduces the *statistics* of the data, not the sequence.
* The mapping is required: a raw table does not say what its columns mean.
  "Any dataset" means any dataset that can be described by a mapping.
* **The residual is not the sensor noise.** A detrender's residual has a different
  std than the white noise that produced it: `rolling_median` inflates it
  (ratio `sqrt(1 + pi/(2m))`, `m` neighbours), `savgol` deflates it
  (`sqrt(1 - c0)`). The ratio is printed and written next to every fit;
  a fitted NORMAL sigma is divided by it before it is written to the `.seg`
  (this assumes the residual is white noise, which is not proven).
* The window is chosen on the train part from a short list. With few windows the
  shopping risk is small but not zero; the hold-out check is the safeguard.
* Hold-out sets can be small (e.g. ~60 daytime hours for one month of irradiance),
  so a pass there is weak evidence.
* Only one node (plus a hub substation and one line) is generated. The hub and line
  are literals; a generated model has no generation/load balance beyond that node.
