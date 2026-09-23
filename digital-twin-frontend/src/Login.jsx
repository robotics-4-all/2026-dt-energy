import React, { useState } from 'react';
//Επιτρέπεται η καθολική εισαγωγή απο το App.jsx
export default function Login({ setToken }) {
    const [credentials, setCredentials] = useState({ email: '', password: '' }); //φτιάχνει κουτάκια μνήμης
    const [error, setError] = useState('');
//η συναρτηση εκτελειται οταν ο μηχανικος πατησει Εισοδο/ enter
// e μας δινει πληροφοριες για το τι κρύβεται στην οθόνη
const handleSubmit = async (e) => {
    //ο browser δεν κάνει refresh την σελίδα    
    e.preventDefault();
        setError('');
//κανουμε έλεγχο οτι ο χρήστης εισάγει το εταιρικό email
        if (!credentials.email.endsWith('@admie.gr')) {
            setError('Επιτρέπεται η είσοδος μόνο με επίσημο εταιρικό email (@admie.gr).');
            return;
        }
//ξεκινάει HTTP αίτημα προς FASTAPI server ,await = "περίμενε πριν πας το επόμενο βήμα"
        try {
            const response = await fetch('http://127.0.0.1:8000/api/auth/login', {
                method: 'POST', //χρησιμοποιώ POST για ευαίσθητα δεδομένα/ δεν φαίνονται στο URL
                headers: { 'Content-Type': 'application/json' }, //μορφή JSON, ενημερώνει το FASTAPI ότι στείλαμε δεδομένα
                body: JSON.stringify({
                    staffId: credentials.email,
                    password: credentials.password //Μετατροπή σε string JSON
                }),
            });

            const data = await response.json();

            if (response.ok && data.access_token) {
                localStorage.setItem('admie_token', data.access_token);
                setToken(data.access_token); //μολις το App.jsx δει ότι το token έχει μνήμη, ανοίγει το Canvas
            } else {
                if (typeof data.detail === 'object') {
                    setError('Σφάλμα ελέγχου δεδομένων από το Backend (422).');
                } else {
                    setError(data.detail || 'Αποτυχία ταυτοποίησης μηχανικού.');
                }
            }
        } catch (err) {
            setError('Αδυναμία σύνδεσης με τον authentication server.');
        }
    };
return (
        <div style={styles.container}> 
            <div style={styles.card}>
                <h2 style={styles.title}>ΑΔΜΗΕ Ψηφιακό Δίδυμο</h2>
                <h4 style={styles.subtitle}>Είσοδος Μηχανικών</h4>
                
                {error && <div style={styles.error}>{error}</div>} 
                
                <form onSubmit={handleSubmit}>
                    <div style={styles.inputGroup}>
                        <label style={styles.label}>Εταιρικό Email</label>
                        <input 
                            type="email" 
                            name="email" 
                            value={credentials.email} 
                            onChange={(e) => setCredentials({ ...credentials, email: e.target.value })} //κάθε φορά που πατάω γράμμα στο πλητρολόγιο, μέσω της React βλέπω live τι γράφεται στην οθόνη
                            required 
                            placeholder="username@admie.gr"
                            style={styles.input}
                        />
                    </div>
                    
                    <div style={styles.inputGroup}>
                        <label style={styles.label}>Κωδικός Πρόσβασης</label>
                        <input 
                            type="password" 
                            name="password" 
                            value={credentials.password} 
                            onChange={(e) => setCredentials({ ...credentials, password: e.target.value })} 
                            required 
                            style={styles.input}
                        />
                    </div>
                    
                    <button type="submit" style={styles.button}>Πιστοποίηση & Είσοδος</button>
                </form>
            </div>
        </div>
    );
}

const styles = {
    container: { 
        display: 'flex', 
        justifyContent: 'center', 
        alignItems: 'center', 
        height: '100vh', 
        backgroundColor: '#000000', 
        fontFamily: 'sans-serif' 
    },
    card: { 
        padding: '30px', 
        backgroundColor: '#1e293b',
        borderRadius: '0px',
        boxShadow: '0 4px 20px rgba(0,0,0,0.8)', 
        width: '400px', 
        color: '#fff' 
    },
    title: { 
        textAlign: 'center', 
        margin: '0 0 5px 0', 
        color: '#00b4d8',
        fontSize: '24px',
        fontWeight: 'bold'
    },
    subtitle: { 
        textAlign: 'center', 
        margin: '0 0 25px 0', 
        color: '#94a3b8',
        fontSize: '14px' 
    },
    inputGroup: { 
        display: 'flex', 
        flexDirection: 'column', 
        marginBottom: '20px' 
    },
    label: { 
        marginBottom: '8px', 
        fontSize: '14px', 
        color: '#f8fafc'
    },
    input: { 
        padding: '12px', 
        borderRadius: '0px',
        border: '1px solid #475569', 
        backgroundColor: '#0f172a',
        color: '#fff', 
        fontSize: '16px' 
    },
    error: { 
        padding: '12px', 
        backgroundColor: '#ef4444', 
        borderRadius: '0px', 
        marginBottom: '20px', 
        fontSize: '14px', 
        textAlign: 'center', 
        fontWeight: 'bold' 
    },
    button: { 
        width: '100%', 
        padding: '14px', 
        backgroundColor: '#0077b6',
        color: '#fff', 
        border: 'none', 
        borderRadius: '0px',
        cursor: 'pointer', 
        fontWeight: 'bold', 
        fontSize: '16px', 
        marginTop: '10px',
        transition: 'background-color 0.2s'
    }
};

// React δεν κανει observe τις απευθείας αλλαγές στις μεταβλητές , χρειαζόμαστε setCredentials.Μόνο μέσω αυτής η React καταλαβαίνει ότι η κατάσταση (State) άλλαξε, ώστε να προκαλέσει ένα re-render (επανασχεδίαση) του UI και να εμφανιστεί live στην οθόνη αυτό που πληκτρολόγησε ο μηχανικός

// Token: FastAPI δεν έχει μνήμη + χρειαζόμαστε ασφάλεια 