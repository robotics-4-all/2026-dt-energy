import argparse
import base64
import hashlib
import logging
import os
import re
import sys
from pprint import pformat
 
from jinja2 import Environment, FileSystemLoader
from textx import metamodel_from_file
from textx.exceptions import TextXError
 
logger = logging.getLogger("generate_twin")
 
DEFAULT_DISTRIBUTIONS = {
    'wind_speed':       {'type': 'WEIBULL',  'params': {'shape': 2.0, 'scale': 7.0}},
    'solar_noise':      {'type': 'NORMAL',   'params': {'mu': 0.0, 'sigma': 0.02}},
    'load_noise':       {'type': 'NORMAL',   'params': {'mu': 0.0, 'sigma': 0.03}},
    'voltage_noise':    {'type': 'NORMAL',   'params': {'mu': 0.0, 'sigma': 0.01}},
    'frequency_noise':  {'type': 'NORMAL',   'params': {'mu': 0.0, 'sigma': 0.005}},
    'soc_noise':        {'type': 'NORMAL',   'params': {'mu': 0.0, 'sigma': 0.01}},
    'congestion_noise': {'type': 'BETA',     'params': {'alpha': 2.0, 'beta': 2.0}},
}
 
APPLICABLE_VARIABLES = {
    'SubStation':     ['congestion_noise', 'voltage_noise'],
    'BatteryStorage': ['soc_noise', 'voltage_noise'],
    'Transformer':    ['voltage_noise'],
    'ThermalPlant':   ['load_noise', 'voltage_noise', 'frequency_noise'],
    'SolarPark':      ['solar_noise', 'voltage_noise', 'frequency_noise'],
    'WindFarm':       ['wind_speed', 'voltage_noise', 'frequency_noise'],
    'Residential':    ['load_noise', 'voltage_noise', 'frequency_noise'],
    'Industrial':     ['load_noise', 'voltage_noise', 'frequency_noise'],
}
 
DIST_CLASS_MAP = {
    'WeibullDist':   ('WEIBULL', ['shape', 'scale']),
    'NormalDist':    ('NORMAL', ['mu', 'sigma']),
    'BetaDist':      ('BETA', ['alpha', 'beta']),
    'LogNormalDist': ('LOGNORMAL', ['mu', 'sigma']),
    'GammaDist':     ('GAMMA', ['shape', 'scale']),
    'PoissonDist':   ('POISSON', ['lam']),
}
 
 
def dist_obj_to_dict(dist_obj):
    dtype, param_names = DIST_CLASS_MAP[dist_obj.__class__.__name__]
    return {
        'type': dtype,
        'params': {p: getattr(dist_obj, p) for p in param_names},
    }
 
 
class DistributionResolver:
 
    def __init__(self, model_root):
        self.global_map = {}
        for block in getattr(model_root, 'globalDistributions', []):
            for entry in block.distributions:
                self.global_map[entry.variable] = entry.distribution
 
        self.class_map = {}
        for block in getattr(model_root, 'classDistributions', []):
            cls_name = block.appliesTo
            self.class_map.setdefault(cls_name, {})
            for entry in block.distributions:
                self.class_map[cls_name][entry.variable] = entry.distribution
 
    def resolve(self, node):
        cls_name = node.__class__.__name__
 
        instance_map = {}
        for entry in getattr(node, 'distributions', []):
            instance_map[entry.variable] = entry.distribution
 
        class_map_for_cls = self.class_map.get(cls_name, {})
        applicable_vars = APPLICABLE_VARIABLES.get(cls_name, list(DEFAULT_DISTRIBUTIONS.keys()))
 
        resolved = {}
        for var in applicable_vars:
            default_cfg = DEFAULT_DISTRIBUTIONS[var]
            if var in instance_map:
                cfg = dist_obj_to_dict(instance_map[var])
                cfg['source'] = 'instance'
            elif var in class_map_for_cls:
                cfg = dist_obj_to_dict(class_map_for_cls[var])
                cfg['source'] = 'class'
            elif var in self.global_map:
                cfg = dist_obj_to_dict(self.global_map[var])
                cfg['source'] = 'global'
            else:
                cfg = {'type': default_cfg['type'], 'params': dict(default_cfg['params']), 'source': 'default'}
            resolved[var] = cfg
        return resolved
 
 
def collect_all_nodes(model_root):
    node_lists = [
        getattr(model_root, 'substations', []),
        getattr(model_root, 'batteries', []),
        getattr(model_root, 'transformers', []),
        getattr(model_root, 'thermalPlants', []),
        getattr(model_root, 'solarParks', []),
        getattr(model_root, 'windFarms', []),
        getattr(model_root, 'residentials', []),
        getattr(model_root, 'industrials', []),
    ]
    nodes = []
    for lst in node_lists:
        nodes.extend(lst)
    return nodes
 
 
def node_ref_id(node):
    return getattr(node, 'elementId', None) or getattr(node, 'id', None) or node.name
 
 
def _first_not_none(*values):
    for v in values:
        if v is not None:
            return v
    return None
 
 
def build_node_summaries(all_nodes, node_class):
    summaries = []
    for node in all_nodes:
        node_id = node.elementId
        max_capacity = _first_not_none(
            getattr(node, 'maxCapacityMW', None),
            getattr(node, 'capacityMWh', None),
            0.0,
        )
        summaries.append({
            'id': node_id,
            'type': node_class[node_id],
            'max_capacity_mw': max_capacity,
            'demand_mw': getattr(node, 'demandMW', 0.0),
            'power_factor': getattr(node, 'powerFactor', 0.9),
            'voltage_level': getattr(node, 'voltageLevel', 20.0),
            'has_smart_appliances': bool(getattr(node, 'hasSmartAppliances', False)),
        })
    return summaries
 
 
def build_all_lines(all_nodes):
    lines = []
    seen = set()
    for node in all_nodes:
        for line in getattr(node, 'lines', []):
            line_id = getattr(line, 'lineId', None) or line.name
            if line_id in seen:
                continue
            seen.add(line_id)
            lines.append({
                'id': line_id,
                'length_km': getattr(line, 'lengthKM', 2.5),
                'max_capacity_mw': getattr(line, 'maxCapacityMW', 2.0),
                'source': node_ref_id(line.source),
                'target': node_ref_id(line.target),
            })
    return lines
 
 
def slugify(text):
    text = text.strip().lower()
    text = re.sub(r'[^a-z0-9]+', '_', text)
    return re.sub(r'_+', '_', text).strip('_') or 'smart_grid'
 
 
def kafka_cluster_id(seed):
    digest = hashlib.sha256(seed.encode('utf-8')).digest()[:16]
    return base64.urlsafe_b64encode(digest).decode('ascii').rstrip('=')
 
 
TEMPLATE_FILES = {
    'edge_simulator.py.jinja':             'edge_simulator.py',
    'persistence_consumer.py.jinja':       'persistence_consumer.py',
    'analytics_consumer.py.jinja':         'analytics_consumer.py',
    'docker-compose.yml.jinja':            'docker-compose.yml',
    'Dockerfile.jinja':                    'Dockerfile',
    'kafka-connect.Dockerfile.jinja':      'kafka-connect.Dockerfile',
    'init_timescale.sql.jinja':            'timescaledb/init.sql',
    'mosquitto.conf.jinja':                'mosquitto/config/mosquitto.conf',
    'kafka_connect_mqtt_telemetry.json.jinja': 'kafka-connect/mqtt-telemetry-source.json',
    'kafka_connect_mqtt_events.json.jinja':    'kafka-connect/mqtt-events-source.json',
    'kafka_connect_mqtt_commands_sink.json.jinja': 'kafka-connect/mqtt-commands-sink.json',
    'requirements.txt.jinja':              'requirements.txt',
    'README.md.jinja':                      'README.md',
    'start_monitor.ps1.jinja':              'start_monitor.ps1',
}
 
 
def log_distribution_report(node_distributions, level=logging.INFO):
    """Καταγράφει (δεν τυπώνει άμεσα) το επίπεδο προέλευσης κάθε κατανομής
    ανά στοιχείο. Χρησιμοποιεί logging ώστε να ελέγχεται η ορατότητά του
    (verbosity) χωρίς να πλημμυρίζει πάντα το stdout."""
    logger.log(level, "Επίπεδο προέλευσης κατανομών ανά στοιχείο:")
    for node_id, dists in node_distributions.items():
        overrides = ", ".join(f"{var}={cfg['source']}" for var, cfg in dists.items())
        logger.log(level, "  %s: %s", node_id, overrides)
 
 
def load_model(model_path, grammar_path):
    """Φορτώνει το grammar και το μοντέλο, με σαφή μηνύματα σφάλματος
    αντί για ωμό traceback αν κάτι δεν υπάρχει ή είναι λάθος διαμορφωμένο."""
    if not os.path.isfile(grammar_path):
        raise FileNotFoundError(f"Δεν βρέθηκε το αρχείο γραμματικής: {grammar_path}")
    if not os.path.isfile(model_path):
        raise FileNotFoundError(f"Δεν βρέθηκε το αρχείο μοντέλου: {model_path}")
 
    try:
        tx_metamodel = metamodel_from_file(grammar_path)
    except TextXError as e:
        raise RuntimeError(f"Σφάλμα στη γραμματική '{grammar_path}': {e}") from e
 
    try:
        model_root = tx_metamodel.model_from_file(model_path)
    except TextXError as e:
        raise RuntimeError(f"Σφάλμα ανάλυσης μοντέλου '{model_path}': {e}") from e
 
    return model_root
 
 
def generate_digital_twin(model_path, templates_dir='templates', grammar_path=None, verbose=False):
    base_name = os.path.splitext(os.path.basename(model_path))[0]
 
    if grammar_path is None:
        grammar_path = os.path.join('grammar', 'smartenergygrid.tx')
 
    model_root = load_model(model_path, grammar_path)
 
    resolver = DistributionResolver(model_root)
    all_nodes = collect_all_nodes(model_root)
 
    if not all_nodes:
        logger.warning("Δεν βρέθηκαν στοιχεία δικτύου στο μοντέλο '%s'.", model_path)
 
    node_distributions = {}
    node_class = {}
    for node in all_nodes:
        node_class[node.elementId] = node.__class__.__name__
        node_distributions[node.elementId] = resolver.resolve(node)
 
    node_summaries = build_node_summaries(all_nodes, node_class)
    all_lines = build_all_lines(all_nodes)
 
    grid = model_root.grids[0] if getattr(model_root, 'grids', None) else None
    grid_name = grid.gridName if (grid and getattr(grid, 'gridName', None)) else base_name
    grid_region = grid.region if (grid and getattr(grid, 'region', None)) else "Unknown Region"
    project_slug = slugify(grid_name)
 
    context = {
        'grid_name': grid_name,
        'grid_region': grid_region,
        'project_slug': project_slug,
        'kafka_cluster_id': kafka_cluster_id(project_slug),
        'node_summaries': node_summaries,
        'all_lines': all_lines,
        'node_distributions': node_distributions,
        'mqtt_topic_prefix': f"grid/telemetry/{project_slug}",
        'mqtt_events_topic': f"grid/events/{project_slug}",
        'mqtt_commands_topic': f"grid/commands/{project_slug}",
        'kafka_topic_raw': 'telemetry.raw',
        'kafka_topic_alerts': 'telemetry.alerts',
        'kafka_topic_events': 'grid.events',
        'kafka_topic_commands': 'control.commands',
    }
 
    if not os.path.isdir(templates_dir):
        raise FileNotFoundError(f"Ο φάκελος templates δεν βρέθηκε: {templates_dir}")
 
    env = Environment(loader=FileSystemLoader(templates_dir))
    env.filters['pyrepr'] = lambda value: pformat(value, indent=4, width=100)
 
    output_root = f"{base_name}_twin"
    for template_name, relative_out in TEMPLATE_FILES.items():
        template = env.get_template(template_name)
        rendered = template.render(**context)
        out_path = os.path.join(output_root, relative_out)
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        encoding = 'utf-8-sig' if out_path.endswith('.ps1') else 'utf-8'
        with open(out_path, 'w', encoding=encoding) as f:
            f.write(rendered)
 
    logger.info("Επιτυχία! Το Digital Twin project παράχθηκε στο φάκελο: %s/", output_root)
    log_distribution_report(node_distributions, level=logging.INFO if verbose else logging.DEBUG)
 
    return output_root
 
 
def parse_args(argv):
    parser = argparse.ArgumentParser(description="Παραγωγή Digital Twin project από μοντέλο .seg")
    parser.add_argument('model_path', help="Διαδρομή προς το αρχείο μοντέλου (.seg)")
    parser.add_argument('--templates-dir', default='templates', help="Φάκελος templates (default: templates)")
    parser.add_argument('--grammar-path', default=None, help="Διαδρομή προς το αρχείο γραμματικής (.tx)")
    parser.add_argument('-v', '--verbose', action='store_true',
                         help="Εμφάνιση αναλυτικού report προέλευσης κατανομών")
    return parser.parse_args(argv)
 
 
def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
 
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )
 
    try:
        generate_digital_twin(
            args.model_path,
            templates_dir=args.templates_dir,
            grammar_path=args.grammar_path,
            verbose=args.verbose,
        )
    except (FileNotFoundError, RuntimeError) as e:
        logger.error("Αποτυχία: %s", e)
        sys.exit(1)
 
 
if __name__ == "__main__":
    main()
 
