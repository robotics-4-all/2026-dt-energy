// useState = μνήμη, useCallback = αποθήκευση συναρτήσεων/ όταν κουναμε components δεν ξαναγράφονται οι συναρτήσεις/ αποφευγονται τα lags
import React, { useState, useCallback, useRef, useEffect } from 'react';
import {
  ReactFlow,
  MiniMap,
  Controls,
  Background,
  useNodesState,
  useEdgesState,
  addEdge,
} from '@xyflow/react';
import Login from './Login'; // Εισαγωγή του Login component

// Στυλ για το React Flow
import '@xyflow/react/dist/style.css';

// Ξεκινάμε με το canvas άδειο για να χτίζει ο μηχανικός από το μηδέν
const initialNodes = [];
const initialEdges = [];

// Μοναδικά ονόματα για τα nodes, ώστε να μην έχουμε conflicts
let idCounter = 0;
const getId = (type) => `${type}_${++idCounter}_${Math.floor(Math.random() * 1000)}`; 

// Κύριο app το οποίο περιέχει ολο το UI του ΑΔΜΗΕ
export default function App() {
  const reactFlowWrapper = useRef(null);
  
  // State για τη διαχείριση του JWT Token ασφαλείας του ΑΔΜΗΕ
  const [token, setToken] = useState(null);

  // Έλεγχος κατά την εκκίνηση αν ο μηχανικός είναι ήδη συνδεδεμένος
  useEffect(() => {
    const savedToken = localStorage.getItem('admie_token');
    if (savedToken) {
      setToken(savedToken);
    }
  }, []);

  // Διαδικασία αποσύνδεσης (Logout)
  const handleLogout = () => {
    localStorage.removeItem('admie_token');
    setToken(null);
  };

  // Ζωντανή μνήμη για nodes/edges, ώστε να αλλάζουν δυναμικά (όταν ο χρήστης κάνει drag/drop ή συνδέσεις)
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const [reactFlowInstance, setReactFlowInstance] = useState(null); // Για να γνωρίζουμε ανα πάσα στιγμή πόσο zoom έχουμε κάνει και προς τα που έχει κουνηθεί το canvas

  // Δημιουργία καλωδίων
  const onConnect = useCallback(
    (params) => setEdges((eds) => addEdge({ ...params, animated: true }, eds)),
    [setEdges],
  );

  // 1. Μόλις ο χρήστης πιάσει ένα κουτί από τη μπάρα, αποθηκεύουμε τον τύπο του (π.χ. WindFarm)
  const onDragStart = (event, nodeType) => {
    event.dataTransfer.setData('application/reactflow', nodeType);
    event.dataTransfer.effectAllowed = 'move';
  };

  const onDragOver = useCallback((event) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = 'move';
  }, []);

  // Το κουμπί που στέλνει το δίκτυο στο FastAPI
  const handleRunTwin = async () => {
    if (nodes.length === 0) {
      alert("Παρακαλώ σχεδιάστε πρώτα ένα δίκτυο σέρνοντας στοιχεία!");
      return;
    }

    const payload = {
      grid_info: {
        name: "React Visual Microgrid",
        region: "Attica Hub"
      },
      nodes: nodes.map(n => ({ id: n.id, type: n.id.split('_')[0], data: n.data })),
      edges: edges
    };

    try {
      const response = await fetch('http://127.0.0.1:8000/api/run-twin', {
        method: 'POST',
        headers: { 
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${token}` // Προσθήκη του Bearer JWT Token για την προστασία του endpoint
        },
        body: JSON.stringify(payload)
      });

      const result = await response.json();
      alert(`Απάντηση από Backend: ${result.message}`);
    } catch (error) {
      alert("Αποτυχία σύνδεσης με τον Python Server. Είναι αναμμένος;");
    }
  };

  // 2. Μόλις ο χρήστης αφήσει το κουτί στο σχεδιαστήριο, υπολογίζουμε τη θέση και το γεννάμε!
  const onDrop = useCallback(
    (event) => {
      event.preventDefault();

      const type = event.dataTransfer.getData('application/reactflow');

      if (!type) return;

      const position = reactFlowInstance.screenToFlowPosition({
        x: event.clientX,
        y: event.clientY,
      });

      let label = '';
      let bg = '';
      
      // Grid Elements
      if (type === 'SubStation') { label = 'SubStation'; bg = '#e74c3c'; }
      if (type === 'BatteryStorage') { label = 'Battery Storage'; bg = '#2ecc71'; }
      if (type === 'Transformer') { label = 'Transformer'; bg = '#9b59b6'; }
      
      // Producers
      if (type === 'ThermalPlant') { label = 'Thermal Plant'; bg = '#e67e22'; }
      if (type === 'SolarPark') { label = 'Solar Park'; bg = '#f1c40f'; }
      if (type === 'WindFarm') { label = 'Wind Farm'; bg = '#3498db'; }
      
      // Consumers
      if (type === 'Residential') { label = 'Residential Load'; bg = '#1abc9c'; }
      if (type === 'Industrial') { label = 'Industrial Load'; bg = '#7f8c8d'; }

      const newNode = {
        id: getId(type),
        type: 'default',
        position,
        data: { label: label },
        style: { 
          background: bg, 
          color: type === 'SolarPark' ? '#333' : 'white', 
          fontWeight: 'bold',
          border: 'none',
          borderRadius: '6px',
          padding: '12px',
          boxShadow: '0 4px 6px rgba(0,0,0,0.1)',
          minWidth: '150px',
          textAlign: 'center'
        },
      };

      setNodes((nds) => nds.concat(newNode));
    },
    [reactFlowInstance, setNodes],
  );

  // Αν δεν υπάρχει έγκυρο token, διακόπτουμε το rendering και δείχνουμε την οθόνη σύνδεσης
  if (!token) {
    return <Login setToken={setToken} />;
  }

  return (
    <div className="dnd-container" style={{ display: 'flex', width: '100vw', height: '100vh', backgroundColor: '#111c24', fontFamily: 'sans-serif' }}>
      {/* Η Πλαϊνή Μπάρα του ΑΔΜΗΕ */}
      <div className="sidebar" style={{ width: '260px', padding: '20px', backgroundColor: '#1a2630', color: 'white', display: 'flex', flexDirection: 'column', overflowY: 'auto', flexShrink: 0, borderRight: '1px solid #2d3748' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '10px' }}>
          <div className="sidebar-title" style={{ margin: 0, fontWeight: 'bold', color: '#4a90e2', fontSize: '18px' }}>ADMHE Control</div>
          <button 
            onClick={handleLogout}
            style={{
              padding: '6px 12px',
              background: '#e74c3c',
              color: 'white',
              border: 'none',
              borderRadius: '4px',
              cursor: 'pointer',
              fontSize: '11px',
              fontWeight: 'bold'
            }}
          >
            Logout
          </button>
        </div>
        <p style={{ fontSize: '12px', color: '#bdc3c7' }}>Σύρετε τα στοιχεία στο σχεδιαστήριο:</p>
        
        <div style={{ fontSize: '11px', fontWeight: 'bold', color: '#7f8c8d', marginTop: '10px', marginBottom: '5px' }}>GRID ELEMENTS</div>
        <div className="dndnode substation" onDragStart={(event) => onDragStart(event, 'SubStation')} draggable style={{ padding: '10px', background: '#e74c3c', color: 'white', margin: '5px 0', cursor: 'grab', fontWeight: 'bold', textAlign: 'center', borderRadius: '4px' }}>
          SubStation
        </div>
        <div className="dndnode battery" onDragStart={(event) => onDragStart(event, 'BatteryStorage')} draggable style={{ padding: '10px', background: '#2ecc71', color: 'white', margin: '5px 0', cursor: 'grab', fontWeight: 'bold', textAlign: 'center', borderRadius: '4px' }}>
          Battery Storage
        </div>
        <div className="dndnode transformer" onDragStart={(event) => onDragStart(event, 'Transformer')} draggable style={{ padding: '10px', background: '#9b59b6', color: 'white', margin: '5px 0', cursor: 'grab', fontWeight: 'bold', textAlign: 'center', borderRadius: '4px' }}>
          Transformer
        </div>

        <div style={{ fontSize: '11px', fontWeight: 'bold', color: '#7f8c8d', marginTop: '15px', marginBottom: '5px' }}>PRODUCERS</div>
        <div className="dndnode thermal" onDragStart={(event) => onDragStart(event, 'ThermalPlant')} draggable style={{ padding: '10px', background: '#e67e22', color: 'white', margin: '5px 0', cursor: 'grab', fontWeight: 'bold', textAlign: 'center', borderRadius: '4px' }}>
          Thermal Plant
        </div>
        <div className="dndnode solarpark" onDragStart={(event) => onDragStart(event, 'SolarPark')} draggable style={{ padding: '10px', background: '#f1c40f', color: '#333', margin: '5px 0', cursor: 'grab', fontWeight: 'bold', textAlign: 'center', borderRadius: '4px' }}>
          Solar Park
        </div>
        <div className="dndnode windfarm" onDragStart={(event) => onDragStart(event, 'WindFarm')} draggable style={{ padding: '10px', background: '#3498db', color: 'white', margin: '5px 0', cursor: 'grab', fontWeight: 'bold', textAlign: 'center', borderRadius: '4px' }}>
          Wind Farm
        </div>

        <div style={{ fontSize: '11px', fontWeight: 'bold', color: '#7f8c8d', marginTop: '15px', marginBottom: '5px' }}>CONSUMERS</div>
        <div className="dndnode residential" onDragStart={(event) => onDragStart(event, 'Residential')} draggable style={{ padding: '10px', background: '#1abc9c', color: 'white', margin: '5px 0', cursor: 'grab', fontWeight: 'bold', textAlign: 'center', borderRadius: '4px' }}>
          Residential Load
        </div>
        <div className="dndnode industrial" onDragStart={(event) => onDragStart(event, 'Industrial')} draggable style={{ padding: '10px', background: '#7f8c8d', color: 'white', margin: '5px 0', cursor: 'grab', fontWeight: 'bold', textAlign: 'center', borderRadius: '4px' }}>
          Industrial Load
        </div>

        <button 
          onClick={handleRunTwin}
          style={{
            marginTop: '30px',
            padding: '12px',
            background: '#e67e22',
            color: 'white',
            border: 'none',
            borderRadius: '6px',
            fontWeight: 'bold',
            cursor: 'pointer',
            width: '100%',
            flexShrink: 0
          }}
        >
          Run Digital Twin
        </button>
      </div>

      {/* Το Ψηφιακό Σχεδιαστήριο */}
      <div className="wrapper" ref={reactFlowWrapper} style={{ flexGrow: 1, height: '100%', position: 'relative' }}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onInit={setReactFlowInstance}
          onDrop={onDrop}
          onDragOver={onDragOver}
          fitView
        >
          <Controls />
          <MiniMap />
          <Background variant="dots" gap={12} size={1} />
        </ReactFlow>
      </div>
    </div>
  );
}