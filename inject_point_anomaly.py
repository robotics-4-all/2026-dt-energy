#!/usr/bin/env python3
"""
inject_point_anomaly.py
========================
Πείραμα 2 — Έγχυση τεχνητής ανωμαλίας στο ζωντανό digital twin.

Τι κάνει (και τίποτα παραπάνω):
  1. Διαβάζει από το TimescaleDB το πιο πρόσφατα ενεργό node_id (αυτό με
     τις πιο πρόσφατες μετρήσεις τάσης) και υπολογίζει τη baseline
     διάμεσο/MAD τάσης του από τα τελευταία VOLTAGE_ZSCORE_WINDOW=30
     δείγματα — ΙΔΙΑ παράθυρο με αυτό που χρησιμοποιεί ο ίδιος ο
     analytics_consumer.py, ώστε η baseline να είναι ρεαλιστική.
  2. Υπολογίζει μία τιμή τάσης "μεμονωμένης ακραίας τιμής" (point
     anomaly): median + INJECT_MAD_MULTIPLIER * mad, όπου
     INJECT_MAD_MULTIPLIER=20 -> z_mod εγγυημένα πολύ πάνω από το
     CRITICAL=3.5 threshold του συστήματος (ώστε να μην υπάρχει
     αμφιβολία αν "θα έπρεπε" να ανιχνευτεί - ξεκάθαρο positive case).
  3. Δημοσιεύει ΕΝΑ μόνο μήνυμα στο Kafka topic πρώτων υλών
     (KAFKA_TOPIC_RAW), με ΑΚΡΙΒΩΣ το ίδιο schema που περιμένει το
     evaluate() του analytics_consumer.py.jinja (payload.get('timestamp'),
     payload['node_id'], payload.get('voltage')) — παρακάμπτοντας τον
     edge_simulator.py, όπως αποφασίστηκε.
  4. Καταγράφει το ground truth (τι εγχύθηκε, πότε, ποιο node) σε έναν
     τοπικό JSON φάκελο results/, ώστε αργότερα να μπορεί να γίνει
     confusion matrix σύγκριση με το predicted_p2/predicted_p1 που θα
     γράψει ο ίδιος ο ζωντανός καταναλωτής στο voltage_zscore_eval.

ΔΕΝ αγγίζει τον edge_simulator.py, ΔΕΝ αλλάζει κώδικα ανίχνευσης, ΔΕΝ
προσθέτει νέα στατιστική μέθοδο· είναι ΜΟΝΟ ένα εργαλείο ελεγχόμενης
έγχυσης δεδομένων για το Πείραμα 2.

Χρήση:
    pip install kafka-python psycopg2-binary
    python inject_point_anomaly.py [--node-id NODE_ID] [--dry-run]

Μεταβλητές περιβάλλοντος (προσαρμόστε αν διαφέρουν στο δικό σας setup):
    KAFKA_BOOTSTRAP_SERVERS   default: localhost:9092
    KAFKA_TOPIC_RAW           default: grid.telemetry.raw   <-- ΕΠΙΒΕΒΑΙΩΣΤΕ!
    TIMESCALE_DSN             default: host=localhost port=5432
                                        dbname=smartgrid_dt user=dt_user
                                        password=dt_password
"""
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    import psycopg2
except ImportError:
    sys.exit("Λείπει το psycopg2-binary. Εγκατάσταση: pip install psycopg2-binary")

try:
    from kafka import KafkaProducer
except ImportError:
    sys.exit("Λείπει το kafka-python. Εγκατάσταση: pip install kafka-python")

# ---- Ρυθμίσεις (ίδιες σημασίας με αυτές του analytics_consumer.py.jinja) ----
KAFKA_BOOTSTRAP_SERVERS = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
# ΠΡΟΣΟΧΗ: επιβεβαιώστε το πραγματικό όνομα topic από το rendered
# docker-compose.yml / analytics_consumer.py του δικού σας project
# (η μεταβλητή jinja ήταν "{{ kafka_topic_raw }}" - το default εδώ είναι
# απλή εικασία σύμβασης ονοματοδοσίας, ΟΧΙ επιβεβαιωμένη τιμή):
KAFKA_TOPIC_RAW = os.environ.get("KAFKA_TOPIC_RAW", "grid.telemetry.raw")
TIMESCALE_DSN = os.environ.get(
    "TIMESCALE_DSN",
    "host=localhost port=5432 dbname=smartgrid_dt user=dt_user password=dt_password",
)

VOLTAGE_ZSCORE_WINDOW = 30      # ίδιο με analytics_consumer.py.jinja
VOLTAGE_MAD_FACTOR = 1.4826     # ίδιο με analytics_consumer.py.jinja
VOLTAGE_ZSCORE_MIN_STD_FRACTION = 0.005  # ίδιο με analytics_consumer.py.jinja
VOLTAGE_ZSCORE_CRITICAL = 3.5   # ίδιο με analytics_consumer.py.jinja
INJECT_MAD_MULTIPLIER = 20      # δικό μας, μόνο για το test-design (βλ. docstring)

RESULTS_DIR = Path(__file__).parent / "experiment2_results"


def pick_node_and_baseline(conn, forced_node_id=None):
    """Επιλέγει node_id (είτε το ζητηθέν, είτε το πιο πρόσφατα ενεργό) και
    υπολογίζει median/MAD τάσης από τα τελευταία 30 δείγματά του — ίδιο
    παράθυρο μεγέθους με το VOLTAGE_ZSCORE_WINDOW του ζωντανού συστήματος."""
    with conn.cursor() as cur:
        if forced_node_id:
            node_id = forced_node_id
        else:
            cur.execute(
                """
                SELECT node_id
                FROM telemetry_metrics_raw
                WHERE voltage IS NOT NULL AND voltage > 0
                ORDER BY "timestamp" DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()
            if row is None:
                sys.exit("Δεν βρέθηκαν πρόσφατες μετρήσεις τάσης σε κανένα node_id.")
            node_id = row[0]

        cur.execute(
            """
            SELECT voltage, node_type
            FROM telemetry_metrics_raw
            WHERE node_id = %s AND voltage IS NOT NULL AND voltage > 0
            ORDER BY "timestamp" DESC
            LIMIT %s
            """,
            (node_id, VOLTAGE_ZSCORE_WINDOW),
        )
        rows = cur.fetchall()
        if len(rows) < 10:
            sys.exit(
                f"Το node_id={node_id} έχει μόνο {len(rows)} πρόσφατα δείγματα "
                f"τάσης (χρειάζονται >=10, όσα και το VOLTAGE_ZSCORE_MIN_SAMPLES "
                f"του analytics_consumer). Δοκιμάστε πάλι σε λίγο ή διαλέξτε άλλο "
                f"node_id με --node-id."
            )
        voltages = [r[0] for r in rows]
        node_type = rows[0][1]

    import statistics
    median_v = statistics.median(voltages)
    mad_v = statistics.median([abs(x - median_v) for x in voltages]) * VOLTAGE_MAD_FACTOR
    mad_floor = abs(median_v) * VOLTAGE_ZSCORE_MIN_STD_FRACTION
    mad_v = max(mad_v, mad_floor)
    return node_id, node_type, median_v, mad_v, voltages


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--node-id", default=None, help="Συγκεκριμένο node_id (αλλιώς: το πιο πρόσφατα ενεργό)")
    parser.add_argument("--dry-run", action="store_true", help="Υπολογισμός/εκτύπωση χωρίς αποστολή στο Kafka")
    args = parser.parse_args()

    conn = psycopg2.connect(TIMESCALE_DSN)
    try:
        node_id, node_type, median_v, mad_v, recent_voltages = pick_node_and_baseline(conn, args.node_id)
    finally:
        conn.close()

    injected_voltage = median_v + INJECT_MAD_MULTIPLIER * mad_v
    implied_z = abs(injected_voltage - median_v) / mad_v

    now = datetime.now(timezone.utc)
    payload = {
        "timestamp": now.isoformat(),
        "node_id": node_id,
        "node_type": node_type,
        "voltage": injected_voltage,
    }

    print("=" * 70)
    print("Πείραμα 2 — Έγχυση μεμονωμένης ακραίας τιμής τάσης (point anomaly)")
    print("=" * 70)
    print(f"node_id επιλογής     : {node_id} (node_type={node_type})")
    print(f"baseline median (V)   : {median_v:.4f}  (από {len(recent_voltages)} πρόσφατα δείγματα)")
    print(f"baseline MAD*1.4826   : {mad_v:.4f}")
    print(f"εγχυόμενη τιμή (V)    : {injected_voltage:.4f}")
    print(f"implied Modified Z    : {implied_z:.2f}  (CRITICAL threshold = {VOLTAGE_ZSCORE_CRITICAL})")
    print(f"timestamp έγχυσης     : {payload['timestamp']}")
    print(f"Kafka topic           : {KAFKA_TOPIC_RAW}")
    print(f"Kafka bootstrap       : {KAFKA_BOOTSTRAP_SERVERS}")
    print("-" * 70)
    print("Αναμενόμενο αποτέλεσμα (βάσει §3.1 του εγγράφου σύγκρισης):")
    print("  predicted_p1 = True   (z_mod > 3.5 σε αυτό το ΜΟΝΟ δείγμα)")
    print("  predicted_p2 = False  (persistence=2 απαιτεί 2 ΣΥΝΕΧΟΜΕΝΑ δείγματα")
    print("                         πάνω από το threshold· η έγχυση είναι 1 μόνο)")
    print("=" * 70)

    if args.dry_run:
        print("[--dry-run] Δεν στάλθηκε τίποτα στο Kafka.")
        return

    producer = KafkaProducer(
        bootstrap_servers=KAFKA_BOOTSTRAP_SERVERS,
        value_serializer=lambda v: json.dumps(v).encode("utf-8"),
    )
    future = producer.send(KAFKA_TOPIC_RAW, payload)
    producer.flush(timeout=10)
    record_metadata = future.get(timeout=10)
    print(
        f"Στάλθηκε: topic={record_metadata.topic} "
        f"partition={record_metadata.partition} offset={record_metadata.offset}"
    )

    RESULTS_DIR.mkdir(exist_ok=True)
    ground_truth_path = RESULTS_DIR / f"injection_{now.strftime('%Y%m%dT%H%M%S')}.json"
    ground_truth = {
        "experiment": "experiment2_point_anomaly",
        "anomaly_type": "point",
        "injected_at_utc": payload["timestamp"],
        "node_id": node_id,
        "node_type": node_type,
        "baseline_median_v": median_v,
        "baseline_mad_v": mad_v,
        "injected_voltage": injected_voltage,
        "implied_z_mod": implied_z,
        "true_anomaly": True,
        "expected_predicted_p1": True,
        "expected_predicted_p2": False,
        "kafka_topic": KAFKA_TOPIC_RAW,
        "kafka_partition": record_metadata.partition,
        "kafka_offset": record_metadata.offset,
    }
    ground_truth_path.write_text(json.dumps(ground_truth, indent=2, ensure_ascii=False))
    print(f"Ground truth αποθηκεύτηκε: {ground_truth_path}")
    print()
    print("Επόμενο βήμα: μετά από λίγα δευτερόλεπτα, ελέγξτε στο")
    print(f"voltage_zscore_eval WHERE node_id='{node_id}' ORDER BY timestamp DESC LIMIT 5;")
    print("αν η γραμμή με αυτό το timestamp έχει predicted_p1=true, predicted_p2=false.")


if __name__ == "__main__":
    main()
