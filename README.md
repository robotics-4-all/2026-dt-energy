# Digital Twin — Smart Energy Grid Generator

Εργαλείο που παράγει αυτόματα ένα πλήρες ψηφιακό δίδυμο για **οποιοδήποτε**
ενεργειακό δίκτυο περιγράφεται σε αρχείο `.seg`. 
Κάθε `.seg` αρχείο παράγει το δικό
του ανεξάρτητο project-φάκελο, με προσομοίωση τηλεμετρίας σε πραγματικό χρόνο
(τάση, συχνότητα, φορτίο κ.λπ.), μεταφορά μέσω MQTT/Kafka, αποθήκευση σε βάση
δεδομένων, και οπτικοποίηση σε dashboards.


## Πώς δουλεύει

```
Edge Simulator  →  MQTT  →  Kafka Connect  →  Kafka  →  Consumers  →  Βάσεις  →  Grafana
(παράγει data)     (μεταφορά μηνυμάτων)              (επεξεργασία)   (αποθήκευση)  (γραφήματα)
```

- Ο **Edge Simulator** παράγει ψεύτικα αλλά ρεαλιστικά δεδομένα για κάθε στοιχείο
  του δικτύου (υποσταθμοί, ανεμογεννήτριες, φωτοβολταϊκά κ.λπ.).
- Τα δεδομένα ταξιδεύουν μέσω **MQTT** και **Kafka** μέχρι δύο "καταναλωτές":
  - ο ένας τα αποθηκεύει σε βάση δεδομένων (**TimescaleDB**)
  - ο άλλος ψάχνει για ανωμαλίες και τις καταγράφει
- Το **Grafana** διαβάζει τη βάση και ζωγραφίζει γραφήματα.

Όλα τρέχουν αυτόματα μέσα σε Docker containers — δεν χρειάζεται να τα στήσεις
ένα-ένα.

## Βήμα 1 — Παρήγαγε το project από το δικό σου `.seg` αρχείο

```bash
python generate_twin.py path/to/model.seg
```

Αυτό δημιουργεί έναν καινούριο φάκελο `<όνομα_αρχείου>_twin/` με όλα τα
απαραίτητα αρχεία (simulator, consumers, docker-compose, κ.λπ.), προσαρμοσμένα
στο δίκτυο που περιγράφει το συγκεκριμένο `.seg` μοντέλο σου. Κάθε `.seg`
αρχείο παράγει ξεχωριστό, ανεξάρτητο φάκελο-project.

Προαιρετικά, με `-v` βλέπεις αναλυτικά από πού προέκυψε κάθε ρύθμιση θορύβου/
κατανομής ανά στοιχείο δικτύου (instance/class/global/default):
```bash
python generate_twin.py path/to/model.seg -v
```

## Βήμα 2 — Μπες στον φάκελο που παράχθηκε και σήκωσε τα containers

```bash
cd <όνομα_αρχείου>_twin
docker compose up -d --build
```

Αυτό είναι το **μόνο** βήμα που χρειάζεται. Σηκώνει όλα τα containers και
ρυθμίζει αυτόματα τις συνδέσεις μεταξύ τους.

**Σε Windows**, μετά την πρώτη φορά μπορείς εναλλακτικά να τρέχεις:
```powershell
.\start_monitor.ps1
```
Σηκώνει τα πάντα *και* ανοίγει δύο παράθυρα με ζωντανά logs (τηλεμετρία +
ανωμαλίες), χωρίς να χρειάζεται να γράφεις εντολές χειροκίνητα.

## Πώς βλέπεις ότι δουλεύει

Δες αν όλα τα containers τρέχουν:
```bash
docker compose ps -a
```

Δες ζωντανά logs από τον simulator:
```bash
docker compose logs -f edge-simulator
```

## Πώς βλέπεις τα γραφήματα (Grafana)

1. Άνοιξε **http://localhost:3000**
2. Login: **admin / admin**
3. Πήγαινε **Connections → Data sources → Add new data source → PostgreSQL**
4. Συμπλήρωσε:
   - Host: `timescaledb:5432`
   - Database: `smartgrid_dt`
   - Username: `dt_user`
   - Password: `dt_password`
   - TLS/SSL Mode: `disable`
   (Αυτά τα credentials τα ορίζει ο generator με τον ίδιο τρόπο σε κάθε
   project — δεν αλλάζουν ανά `.seg` αρχείο.)
5. Πάτα **Save & test**
6. Φτιάξε νέο panel (**Dashboards → New → New dashboard → Add visualization**),
   διάλεξε το data source σου, και βάλε ένα query όπως:
   ```sql
   SELECT
     timestamp AS time,
     node_id,
     voltage
   FROM telemetry_metrics_raw
   WHERE $__timeFilter(timestamp)
   ORDER BY timestamp
   ```