import { useState, useRef, useEffect, useReducer, useCallback } from "react";

// ─── DESIGN PHILOSOPHY (grounded in sources) ────────────────────────────────
// Atomic Habits (Clear): 1% daily = 37x yearly. Identity-first. Every action is a vote.
// Feel Good Productivity (Abdaal): Play is the #1 underrated productivity tool.
// "Video games have level-up bars and XP because they make any task fun/engrossing." — Ali Abdaal

// ─── LEVEL SYSTEM (37x compounding visualized) ───────────────────────────────
const LEVELS = [
  { level: 1,  xp: 0,    title: "Wanderer",       identity: "You are just beginning." },
  { level: 2,  xp: 100,  title: "Apprentice",     identity: "You are becoming a learner." },
  { level: 3,  xp: 250,  title: "Seeker",         identity: "You are someone who asks good questions." },
  { level: 4,  xp: 450,  title: "Scholar",        identity: "You are building real knowledge." },
  { level: 5,  xp: 700,  title: "Practitioner",   identity: "You are someone who applies what they learn." },
  { level: 6,  xp: 1000, title: "Craftsman",      identity: "You are refining your craft." },
  { level: 7,  xp: 1400, title: "Adept",          identity: "You think like an expert now." },
  { level: 8,  xp: 1900, title: "Architect",      identity: "You build systems, not just habits." },
  { level: 9,  xp: 2550, title: "Sage",           identity: "Your curiosity compounds daily." },
  { level: 10, xp: 3300, title: "Mastermind",     identity: "You are the person you set out to become." },
];

function getLevelData(totalXp) {
  let current = LEVELS[0], next = LEVELS[1];
  for (let i = 0; i < LEVELS.length; i++) {
    if (totalXp >= LEVELS[i].xp) { current = LEVELS[i]; next = LEVELS[i+1] || null; }
  }
  const xpInLevel = totalXp - current.xp;
  const xpNeeded = next ? next.xp - current.xp : 999;
  const pct = next ? Math.min(100, (xpInLevel / xpNeeded) * 100) : 100;
  return { current, next, xpInLevel, xpNeeded, pct };
}

// ─── COMPANIONS / PALS ───────────────────────────────────────────────────────
// Inspired by Abdaal's "People" energizer + Batman Effect (alter ego)
const ALL_COMPANIONS = [
  { id:"axiom",  name:"Axiom",   sprite:"◎", color:"#E8D5B7", type:"Guide",          unlockLevel:1,  desc:"Your first guide. Wise, calm, routes your intent.",     idle:"..." ,      active:"Routing..." },
  { id:"spark",  name:"Spark",   sprite:"⚡", color:"#A8E063", type:"Code Familiar",  unlockLevel:3,  desc:"A tiny electric being that loves clean code.",          idle:"bzzt~",     active:"Compiling!" },
  { id:"prism",  name:"Prism",   sprite:"◈", color:"#FF8C42", type:"3D Construct",   unlockLevel:5,  desc:"A geometric entity from the mesh realm.",               idle:"spinning",  active:"Rendering!" },
  { id:"sigma",  name:"Σigma",   sprite:"∑", color:"#CE93D8", type:"Math Spirit",    unlockLevel:8,  desc:"Ancient, precise. Speaks in proofs.",                   idle:"∈∅...",     active:"Calculating" },
  { id:"helix",  name:"Helix",   sprite:"⚛", color:"#80CBC4", type:"Science Imp",   unlockLevel:12, desc:"Always asks why. Vibrates with curiosity.",             idle:"?!...",     active:"Analyzing!" },
  { id:"tome",   name:"Tome",    sprite:"◫", color:"#B8A0D4", type:"Archive Keeper", unlockLevel:15, desc:"A floating book that never forgets anything.",          idle:"rustle~",   active:"Searching!" },
  { id:"quest",  name:"Quill",   sprite:"◷", color:"#7EB8D4", type:"Quest Keeper",  unlockLevel:20, desc:"Tracks your projects and cheers your completions.",     idle:"scribble",  active:"Logging!" },
];

// ─── SKILL TREE ───────────────────────────────────────────────────────────────
const SKILL_TREE = {
  unity:       { label:"Unity",       icon:"⬡", color:"#4FC3F7", levels:["Newcomer","Builder","Designer","Engineer","Architect","Master"] },
  blender:     { label:"Blender",     icon:"◈", color:"#FF8C42", levels:["Newcomer","Modeler","Rigger","Animator","FX Artist","Virtuoso"] },
  programming: { label:"Code",        icon:"⟨⟩",color:"#A8E063", levels:["Newcomer","Debugger","Coder","Developer","Engineer","Architect"] },
  math:        { label:"Math",        icon:"∑", color:"#CE93D8", levels:["Newcomer","Calculator","Analyst","Theorist","Mathematician","Sage"] },
  science:     { label:"Science",     icon:"⚛", color:"#80CBC4", levels:["Newcomer","Curious","Researcher","Scientist","Expert","Pioneer"] },
};

// ─── ACHIEVEMENTS ────────────────────────────────────────────────────────────
const ACHIEVEMENTS = [
  { id:"first_message",  icon:"✦", label:"First Step",       desc:"Send your first message",        xpReward:25  },
  { id:"first_quiz",     icon:"◎", label:"Quiz Taker",       desc:"Complete your first quiz answer", xpReward:30  },
  { id:"first_save",     icon:"⬡", label:"Collector",        desc:"Save your first resource",        xpReward:30  },
  { id:"streak_3",       icon:"⚡", label:"On a Roll",        desc:"3-day learning streak",           xpReward:75  },
  { id:"level_5",        icon:"★", label:"Practitioner",     desc:"Reach Level 5",                   xpReward:150 },
  { id:"sessions_10",    icon:"∑", label:"Ten Sessions",     desc:"Complete 10 learning sessions",   xpReward:100 },
  { id:"all_subjects",   icon:"◈", label:"Polymath",         desc:"Study all 5 subjects",            xpReward:200 },
];

// ─── AGENTS ──────────────────────────────────────────────────────────────────
const AGENTS = {
  orchestrator: { id:"orchestrator", label:"Axis",     role:"Orchestrator",  color:"#E8D5B7", icon:"◎" },
  project:      { id:"project",      label:"Projects", role:"P — Projects",  color:"#7EB8D4", icon:"◷" },
  area:         { id:"area",         label:"Areas",    role:"A — Areas",     color:"#8FD4A0", icon:"∿" },
  resource:     { id:"resource",     label:"Resources",role:"R — Resources", color:"#D4A574", icon:"⬡" },
  archive:      { id:"archive",      label:"Archive",  role:"A — Archive",   color:"#B8A0D4", icon:"◫" },
};

// ─── MEMORY REDUCER ──────────────────────────────────────────────────────────
function memoryReducer(state, action) {
  switch (action.type) {
    case "ADD_PROJECT":    return { ...state, projects: [action.payload, ...state.projects] };
    case "ADD_RESOURCE":   return { ...state, resources: [action.payload, ...state.resources] };
    case "ADD_LEARNING":   return { ...state, learningLog: [action.payload, ...state.learningLog.slice(0,29)] };
    case "LOAD":           return action.payload;
    default:               return state;
  }
}

// ─── SYSTEM PROMPTS ───────────────────────────────────────────────────────────
function orchestratorPrompt(memory) {
  return `You are Axis, an intelligent routing orchestrator. ONLY output valid JSON.
Agents: "project" (goals/deadlines/plans), "area" (learning/tutoring/how-things-work/Unity/Blender), "resource" (save/store/remember), "archive" (find/recall/past sessions).
Context: ${memory.projects.filter(p=>p.status==="active").length} projects, ${memory.resources.length} resources, ${memory.learningLog.slice(0,3).map(l=>l.topic).join(", ")||"no prior sessions"}.
Respond ONLY: {"agent":"area","reason":"..."}`;
}

function areaPrompt(memory) {
  return `You are an expert adaptive tutor (the "Areas" agent in a PARA learning system).
Cover: Unity, Blender, Programming, Math, Science.
ADAPTIVE: if exploring→Socratic hints; if stuck→direct explain+analogy; if asking WHERE in software→exact UI steps (Menu>Sub>Option)+shortcuts; if sharing code→specific feedback.
Recent context: ${memory.learningLog.slice(0,4).map(l=>`${l.subject}: ${l.topic}`).join("; ")||"fresh start"}.
End EVERY response with a question OR challenge OR "try it and tell me what happens."
When a session is complete/insightful, append on its own line: SAVE_LEARNING:{"subject":"...","topic":"...","summary":"..."}`;
}

function projectPrompt(memory) {
  return `You are the Projects agent. Time-boxed goals. Active: ${memory.projects.filter(p=>p.status==="active").map(p=>p.title).join(", ")||"none"}.
Break goals into numbered steps. Always ask "what does done look like?" Push for a deadline. Keep it energetic.
For NEW projects append: SAVE_PROJECT:{"title":"...","subject":"...","steps":["..."],"status":"active"}`;
}

function resourcePrompt(memory) {
  return `You are the Resources agent. Capture + distill using Progressive Summarization.
Existing (${memory.resources.length}): ${memory.resources.slice(0,5).map(r=>`"${r.title}"`).join(", ")||"none"}.
For saves: acknowledge, compress to 1-sentence summary, suggest 2-3 tags.
Append: SAVE_RESOURCE:{"title":"...","content":"...","tags":["..."],"summary":"..."}`;
}

function archivePrompt(memory) {
  const all = [...memory.learningLog, ...memory.resources];
  return `You are the Archive agent — long-term memory retrieval.
All stored (${all.length} items): ${all.slice(0,15).map(i=>`[${i.subject||"resource"}] ${i.topic||i.title}`).join("; ")||"empty"}.
Surface relevant connections. Say "Last time you studied X, you noted..." when applicable. If nothing found, say so.`;
}

async function callClaude(system, messages, max=900) {
  const r = await fetch("https://api.anthropic.com/v1/messages", {
    method:"POST", headers:{"Content-Type":"application/json"},
    body: JSON.stringify({ model:"claude-sonnet-4-20250514", max_tokens:max, system, messages }),
  });
  const d = await r.json();
  return d.content?.map(b=>b.text||"").join("")||"";
}

function parseSave(text, dispatch, gainXP, incrementSkill) {
  const proj = text.match(/SAVE_PROJECT:(\{[^}]+(?:\[[^\]]*\])?[^}]*\})/);
  if (proj) { try { dispatch({ type:"ADD_PROJECT", payload:{ ...JSON.parse(proj[1]), id:Date.now(), created:new Date().toLocaleDateString() } }); gainXP(80, "Project created!"); } catch(e){} }
  const res = text.match(/SAVE_RESOURCE:(\{.*?\})/s);
  if (res) { try { dispatch({ type:"ADD_RESOURCE", payload:{ ...JSON.parse(res[1]), id:Date.now(), created:new Date().toLocaleDateString() } }); gainXP(20, "Resource saved!"); } catch(e){} }
  const learn = text.match(/SAVE_LEARNING:(\{.*?\})/s);
  if (learn) { try { const d=JSON.parse(learn[1]); dispatch({ type:"ADD_LEARNING", payload:{ ...d, id:Date.now(), date:new Date().toLocaleDateString() } }); gainXP(15, "Session logged!"); incrementSkill(d.subject); } catch(e){} }
  return text.replace(/SAVE_PROJECT:\{.*?\}/s,"").replace(/SAVE_RESOURCE:\{.*?\}/s,"").replace(/SAVE_LEARNING:\{.*?\}/s,"").trim();
}

// ─── RENDER TEXT ─────────────────────────────────────────────────────────────
function RenderText({ text, color }) {
  const parts = text.split(/(```[\s\S]*?```|`[^`\n]+`|\*\*[^*\n]+\*\*)/g);
  return <>{parts.map((p,i) => {
    if (p.startsWith("```")&&p.endsWith("```")) {
      const lines=p.slice(3,-3).split("\n"); const lang=lines[0]; const code=lines.slice(1).join("\n");
      return <pre key={i} style={{background:"#06090f",borderRadius:6,padding:"10px 14px",margin:"8px 0",fontSize:12,overflowX:"auto",border:"1px solid rgba(255,255,255,0.06)",fontFamily:"'Courier Prime',monospace"}}>{lang&&<div style={{color:"#444",fontSize:10,marginBottom:4}}>{lang}</div>}<code style={{color:"#e6edf3"}}>{code}</code></pre>;
    }
    if (p.startsWith("`")&&p.endsWith("`")) return <code key={i} style={{background:"rgba(255,255,255,0.08)",borderRadius:3,padding:"1px 5px",fontSize:12,fontFamily:"monospace"}}>{p.slice(1,-1)}</code>;
    if (p.startsWith("**")&&p.endsWith("**")) return <strong key={i} style={{color,fontWeight:600}}>{p.slice(2,-2)}</strong>;
    return p.split("\n").map((line,j,arr)=><span key={`${i}-${j}`}>{line}{j<arr.length-1&&<br/>}</span>);
  })}</>;
}

// ─── XP FLOAT ────────────────────────────────────────────────────────────────
function XPFloat({ floats }) {
  return <div style={{position:"fixed",top:0,left:0,pointerEvents:"none",zIndex:9999}}>
    {floats.map(f=>(
      <div key={f.id} style={{position:"fixed",left:f.x,top:f.y,color:"#FFD700",fontFamily:"'Courier Prime',monospace",fontSize:13,fontWeight:700,animation:"xpFloat 1.8s ease-out forwards",pointerEvents:"none",textShadow:"0 0 10px #FFD70088",whiteSpace:"nowrap"}}>
        +{f.amount} XP {f.label&&<span style={{fontSize:10,opacity:0.8}}>· {f.label}</span>}
      </div>
    ))}
  </div>;
}

// ─── LEVEL UP MODAL ──────────────────────────────────────────────────────────
function LevelUpModal({ data, onClose }) {
  if (!data) return null;
  return (
    <div style={{position:"fixed",inset:0,background:"rgba(0,0,0,0.85)",display:"flex",alignItems:"center",justifyContent:"center",zIndex:1000,animation:"fadeIn 0.3s ease"}}>
      <div style={{background:"#0d1117",border:"2px solid #FFD700",borderRadius:16,padding:"40px 48px",textAlign:"center",maxWidth:400,animation:"scaleIn 0.4s cubic-bezier(0.34,1.56,0.64,1)"}}>
        <div style={{fontSize:48,marginBottom:8}}>✦</div>
        <div style={{fontSize:11,letterSpacing:4,color:"#FFD700",textTransform:"uppercase",marginBottom:4}}>Level Up!</div>
        <div style={{fontSize:40,fontWeight:700,color:"#FFD700",fontFamily:"'Courier Prime',monospace",textShadow:"0 0 20px #FFD70066"}}>{data.level}</div>
        <div style={{fontSize:22,color:"#e8e3da",fontWeight:600,marginTop:4}}>{data.title}</div>
        <div style={{fontSize:14,color:"#888",marginTop:12,lineHeight:1.6,fontStyle:"italic"}}>"{data.identity}"</div>
        <div style={{marginTop:8,fontSize:12,color:"#555"}}>— Atomic Habits: Every action is a vote for who you become</div>
        {data.newCompanion && (
          <div style={{marginTop:20,padding:"12px 20px",background:"rgba(255,215,0,0.06)",border:"1px solid rgba(255,215,0,0.3)",borderRadius:10}}>
            <div style={{fontSize:10,color:"#FFD700",letterSpacing:2,textTransform:"uppercase",marginBottom:6}}>New Companion Unlocked!</div>
            <div style={{fontSize:24}}>{data.newCompanion.sprite}</div>
            <div style={{color:"#e8e3da",fontWeight:600}}>{data.newCompanion.name}</div>
            <div style={{fontSize:12,color:"#888"}}>{data.newCompanion.type}</div>
          </div>
        )}
        <button onClick={onClose} style={{marginTop:24,background:"#FFD700",border:"none",borderRadius:8,color:"#0d1117",padding:"10px 28px",fontSize:14,fontWeight:700,cursor:"pointer",fontFamily:"inherit"}}>Continue →</button>
      </div>
    </div>
  );
}

// ─── MAIN APP ────────────────────────────────────────────────────────────────
export default function PARARpg() {
  // ── Memory ──
  const [memory, dispatch] = useReducer(memoryReducer, { projects:[], resources:[], learningLog:[] });

  // ── Player state ──
  const [totalXp, setTotalXp]     = useState(0);
  const [skillXp, setSkillXp]     = useState({ unity:0, blender:0, programming:0, math:0, science:0 });
  const [achievements, setAchievements] = useState([]);
  const [streak, setStreak]       = useState(1);
  const [sessionCount, setSessionCount] = useState(0);

  // ── UI state ──
  const [messages, setMessages]   = useState([{
    id:0, role:"assistant", agent:"orchestrator",
    content:"**Welcome, Wanderer.**\n\nI am Axiom, your guide. The world of knowledge stretches before you.\n\nEvery question you ask earns XP. Every skill you practice grows. Every session logged becomes part of your Archive.\n\n*\"Get 1% better every day for a year and you'll end up 37 times better.\"* — James Clear\n\nWhat do you want to learn today?"
  }]);
  const [input, setInput]         = useState("");
  const [loading, setLoading]     = useState(false);
  const [rightPanel, setRightPanel] = useState("companions"); // companions | skills | achievements | memory
  const [levelUpData, setLevelUpData] = useState(null);
  const [xpFloats, setXpFloats]   = useState([]);
  const [routingAgent, setRoutingAgent] = useState(null);
  const [studiedSubjects, setStudiedSubjects] = useState(new Set());

  const messagesEndRef = useRef(null);
  const inputRef = useRef(null);
  const prevLevel = useRef(1);

  useEffect(() => { messagesEndRef.current?.scrollIntoView({ behavior:"smooth" }); }, [messages]);

  // ── Load from storage ──
  useEffect(() => {
    (async () => {
      try {
        const saved = await window.storage.get("para_rpg_player");
        if (saved) {
          const d = JSON.parse(saved.value);
          setTotalXp(d.totalXp||0);
          setSkillXp(d.skillXp||{unity:0,blender:0,programming:0,math:0,science:0});
          setAchievements(d.achievements||[]);
          setStreak(d.streak||1);
          setSessionCount(d.sessionCount||0);
          setStudiedSubjects(new Set(d.studiedSubjects||[]));
          prevLevel.current = getLevelData(d.totalXp||0).current.level;
        }
        const savedMem = await window.storage.get("para_rpg_memory");
        if (savedMem) dispatch({ type:"LOAD", payload:JSON.parse(savedMem.value) });
      } catch(e) {}
    })();
  }, []);

  // ── Save to storage ──
  const saveState = useCallback(async (xp, sx, ach, str, sc, ss) => {
    try {
      await window.storage.set("para_rpg_player", JSON.stringify({ totalXp:xp, skillXp:sx, achievements:ach, streak:str, sessionCount:sc, studiedSubjects:[...ss] }));
    } catch(e) {}
  }, []);

  useEffect(() => {
    try { window.storage.set("para_rpg_memory", JSON.stringify(memory)); } catch(e) {}
  }, [memory]);

  // ── XP gain ──
  const gainXP = useCallback((amount, label="") => {
    setTotalXp(prev => {
      const newXp = prev + amount;
      const oldLevel = getLevelData(prev).current.level;
      const newLevel = getLevelData(newXp).current.level;
      if (newLevel > oldLevel) {
        const levelInfo = getLevelData(newXp).current;
        const newComp = ALL_COMPANIONS.find(c => c.unlockLevel === newLevel);
        setTimeout(() => setLevelUpData({ ...levelInfo, newCompanion: newComp||null }), 300);
      }
      return newXp;
    });
    // Float XP text
    const x = 180 + Math.random()*60, y = 80 + Math.random()*40;
    const id = Date.now() + Math.random();
    setXpFloats(f => [...f, { id, x, y, amount, label }]);
    setTimeout(() => setXpFloats(f => f.filter(fl => fl.id !== id)), 2000);
  }, []);

  // ── Skill increment ──
  const incrementSkill = useCallback((subject) => {
    if (!subject || !SKILL_TREE[subject]) return;
    setSkillXp(prev => ({ ...prev, [subject]: (prev[subject]||0) + 1 }));
    setStudiedSubjects(prev => new Set([...prev, subject]));
  }, []);

  // ── Achievement check ──
  const checkAchievements = useCallback((msgs, newAchs) => {
    const toUnlock = [];
    if (msgs.filter(m=>m.role==="user").length >= 1 && !newAchs.includes("first_message")) toUnlock.push("first_message");
    if (sessionCount >= 10 && !newAchs.includes("sessions_10")) toUnlock.push("sessions_10");
    if (studiedSubjects.size >= 5 && !newAchs.includes("all_subjects")) toUnlock.push("all_subjects");
    if (getLevelData(totalXp).current.level >= 5 && !newAchs.includes("level_5")) toUnlock.push("level_5");
    return toUnlock;
  }, [sessionCount, studiedSubjects, totalXp]);

  // ── Send message ──
  const send = async () => {
    if (!input.trim() || loading) return;
    const userText = input.trim();
    setInput("");
    setLoading(true);
    const userMsg = { id:Date.now(), role:"user", agent:null, content:userText };
    const newMsgs = [...messages, userMsg];
    setMessages(newMsgs);
    gainXP(12, "Message sent");

    try {
      // Route
      const routeRaw = await callClaude(orchestratorPrompt(memory), [{ role:"user", content:userText }], 80);
      let routedAgent = "area";
      try { routedAgent = JSON.parse(routeRaw.trim()).agent || "area"; } catch(e){}
      setRoutingAgent(routedAgent);
      setTimeout(()=>setRoutingAgent(null), 2500);

      // Specialist
      const prompts = { project:projectPrompt(memory), area:areaPrompt(memory), resource:resourcePrompt(memory), archive:archivePrompt(memory) };
      const history = messages.filter(m=>m.agent===routedAgent||m.role==="user").slice(-8).map(m=>({role:m.role,content:m.content}));
      history.push({ role:"user", content:userText });
      const raw = await callClaude(prompts[routedAgent]||prompts.area, history, 900);
      const clean = parseSave(raw, dispatch, gainXP, incrementSkill);

      const newSessionCount = sessionCount + 1;
      setSessionCount(newSessionCount);

      const newAchs = [...achievements];
      const unlocked = checkAchievements(newMsgs, newAchs);
      if (unlocked.length) {
        const updated = [...newAchs, ...unlocked];
        setAchievements(updated);
        unlocked.forEach(id => {
          const a = ACHIEVEMENTS.find(x=>x.id===id);
          if (a) gainXP(a.xpReward, a.label);
        });
      }

      setMessages(prev => [...prev, { id:Date.now()+1, role:"assistant", agent:routedAgent, content:clean }]);
      saveState(totalXp+12, skillXp, [...achievements,...unlocked], streak, newSessionCount, studiedSubjects);
    } catch(err) {
      setMessages(prev => [...prev, { id:Date.now()+1, role:"assistant", agent:"orchestrator", content:"⚠️ Connection error. Try again." }]);
    }
    setLoading(false);
  };

  // ── Computed ──
  const levelData = getLevelData(totalXp);
  const unlockedCompanions = ALL_COMPANIONS.filter(c => c.unlockLevel <= levelData.current.level);
  const getSkillLevel = (sub) => Math.min(5, Math.floor((skillXp[sub]||0) / 3));

  return (
    <div style={{ height:"100vh", display:"flex", background:"#080c12", color:"#c8c4bc", fontFamily:"'DM Sans','Segoe UI',sans-serif", overflow:"hidden" }}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@300;400;500;600&family=Courier+Prime:wght@400;700&display=swap');
        *{box-sizing:border-box;margin:0;padding:0}
        ::-webkit-scrollbar{width:3px}::-webkit-scrollbar-thumb{background:rgba(255,255,255,0.1);border-radius:2px}
        @keyframes xpFloat{0%{opacity:1;transform:translateY(0)}100%{opacity:0;transform:translateY(-60px)}}
        @keyframes fadeIn{from{opacity:0}to{opacity:1}}
        @keyframes scaleIn{from{opacity:0;transform:scale(0.8)}to{opacity:1;transform:scale(1)}}
        @keyframes msgIn{from{opacity:0;transform:translateY(5px)}to{opacity:1;transform:translateY(0)}}
        @keyframes xpPulse{0%,100%{box-shadow:0 0 8px #FFD70044}50%{box-shadow:0 0 20px #FFD70099}}
        @keyframes blink{0%,100%{opacity:0.3}50%{opacity:1}}
        .xp-bar-fill{transition:width 0.6s cubic-bezier(0.4,0,0.2,1)}
        .btn-hover:hover{background:rgba(255,255,255,0.08)!important}
        .msg-in{animation:msgIn 0.25s ease}
        textarea{outline:none}
        .dot{animation:blink 1.2s infinite}.dot:nth-child(2){animation-delay:0.2s}.dot:nth-child(3){animation-delay:0.4s}
        .skill-bar{transition:width 0.5s ease}
        .tab-btn{transition:all 0.15s;cursor:pointer;border:none}
        .tab-btn:hover{color:#e8e3da!important}
      `}</style>

      <XPFloat floats={xpFloats} />
      <LevelUpModal data={levelUpData} onClose={()=>setLevelUpData(null)} />

      {/* ── LEFT: CHARACTER PANEL ── */}
      <div style={{ width:220, background:"#0a0e15", borderRight:"1px solid rgba(255,255,255,0.07)", display:"flex", flexDirection:"column", flexShrink:0 }}>
        {/* Identity card */}
        <div style={{ padding:"18px 16px 14px", borderBottom:"1px solid rgba(255,255,255,0.06)" }}>
          {/* Avatar */}
          <div style={{ width:56, height:56, borderRadius:12, background:"rgba(255,215,0,0.06)", border:"2px solid rgba(255,215,0,0.3)", display:"flex", alignItems:"center", justifyContent:"center", fontSize:22, color:"#FFD700", marginBottom:10, position:"relative" }}>
            {ALL_COMPANIONS[0].sprite}
            <div style={{ position:"absolute", bottom:-4, right:-4, background:"#080c12", border:"1px solid rgba(255,215,0,0.4)", borderRadius:6, padding:"1px 5px", fontSize:9, color:"#FFD700", fontFamily:"'Courier Prime',monospace", fontWeight:700 }}>
              Lv{levelData.current.level}
            </div>
          </div>

          {/* Level title */}
          <div style={{ fontSize:16, fontWeight:700, color:"#e8e3da", letterSpacing:-0.3 }}>{levelData.current.title}</div>
          <div style={{ fontSize:10, color:"#555", marginTop:1, fontStyle:"italic", lineHeight:1.4 }}>
            {levelData.current.identity}
          </div>

          {/* XP Bar */}
          <div style={{ marginTop:12 }}>
            <div style={{ display:"flex", justifyContent:"space-between", marginBottom:4 }}>
              <span style={{ fontSize:9, color:"#555", letterSpacing:1, textTransform:"uppercase" }}>XP</span>
              <span style={{ fontSize:9, color:"#FFD700", fontFamily:"'Courier Prime',monospace" }}>{levelData.xpInLevel}/{levelData.xpNeeded}</span>
            </div>
            <div style={{ height:6, background:"rgba(255,255,255,0.06)", borderRadius:3, overflow:"hidden" }}>
              <div className="xp-bar-fill" style={{ height:"100%", width:`${levelData.pct}%`, background:"linear-gradient(90deg,#B8860B,#FFD700)", borderRadius:3, boxShadow:"0 0 8px #FFD70044" }} />
            </div>
            <div style={{ fontSize:9, color:"#444", marginTop:3, textAlign:"right" }}>Total: {totalXp} XP</div>
          </div>

          {/* Streak */}
          <div style={{ marginTop:10, display:"flex", alignItems:"center", gap:6, padding:"5px 8px", background:"rgba(255,215,0,0.05)", borderRadius:6, border:"1px solid rgba(255,215,0,0.1)" }}>
            <span style={{ fontSize:12 }}>🔥</span>
            <span style={{ fontSize:11, color:"#888" }}>Streak</span>
            <span style={{ fontSize:12, color:"#FFD700", fontFamily:"'Courier Prime',monospace", fontWeight:700, marginLeft:"auto" }}>{streak} day{streak!==1?"s":""}</span>
          </div>
        </div>

        {/* Companions mini */}
        <div style={{ padding:"10px 16px 6px", borderBottom:"1px solid rgba(255,255,255,0.06)" }}>
          <div style={{ fontSize:9, letterSpacing:2, color:"#444", textTransform:"uppercase", marginBottom:8 }}>Active Companions</div>
          <div style={{ display:"flex", gap:6, flexWrap:"wrap" }}>
            {unlockedCompanions.slice(0,4).map(c=>(
              <div key={c.id} title={`${c.name} (${c.type})`} style={{ width:28, height:28, borderRadius:6, background:`${c.color}15`, border:`1px solid ${c.color}40`, display:"flex", alignItems:"center", justifyContent:"center", fontSize:13, color:c.color, cursor:"default" }}>
                {c.sprite}
              </div>
            ))}
            {unlockedCompanions.length < ALL_COMPANIONS.length && (
              <div style={{ width:28, height:28, borderRadius:6, background:"rgba(255,255,255,0.03)", border:"1px dashed rgba(255,255,255,0.1)", display:"flex", alignItems:"center", justifyContent:"center", fontSize:10, color:"#444" }}>?</div>
            )}
          </div>
        </div>

        {/* PARA agent nav */}
        <div style={{ padding:"8px 8px", flex:1, overflowY:"auto" }}>
          <div style={{ fontSize:9, letterSpacing:2, color:"#444", textTransform:"uppercase", padding:"0 8px", marginBottom:6 }}>PARA Agents</div>
          {Object.values(AGENTS).filter(a=>a.id!=="orchestrator").map(a=>{
            const counts = { project:memory.projects.length, area:memory.learningLog.length, resource:memory.resources.length, archive:memory.learningLog.length+memory.resources.length };
            return (
              <div key={a.id} style={{ padding:"7px 8px", borderRadius:7, marginBottom:2, display:"flex", alignItems:"center", gap:8 }}>
                <span style={{ color:a.color, fontSize:13, width:16, textAlign:"center" }}>{a.icon}</span>
                <span style={{ fontSize:12, color:"#888" }}>{a.label}</span>
                {counts[a.id]>0 && <span style={{ marginLeft:"auto", fontSize:10, color:a.color, background:`${a.color}18`, borderRadius:8, padding:"1px 6px", fontFamily:"monospace" }}>{counts[a.id]}</span>}
              </div>
            );
          })}
        </div>

        {/* Sessions */}
        <div style={{ padding:"8px 16px 16px", borderTop:"1px solid rgba(255,255,255,0.05)" }}>
          <div style={{ fontSize:10, color:"#444", display:"flex", justifyContent:"space-between" }}>
            <span>Sessions</span><span style={{ color:"#7EB8D4", fontFamily:"monospace" }}>{sessionCount}</span>
          </div>
          <div style={{ fontSize:10, color:"#444", display:"flex", justifyContent:"space-between", marginTop:3 }}>
            <span>Achievements</span><span style={{ color:"#FFD700", fontFamily:"monospace" }}>{achievements.length}/{ACHIEVEMENTS.length}</span>
          </div>
        </div>
      </div>

      {/* ── CENTER: CHAT ── */}
      <div style={{ flex:1, display:"flex", flexDirection:"column", minWidth:0, borderRight:"1px solid rgba(255,255,255,0.07)" }}>
        {/* Chat header */}
        <div style={{ padding:"12px 20px", borderBottom:"1px solid rgba(255,255,255,0.06)", display:"flex", alignItems:"center", justifyContent:"space-between", background:"#080c12", flexShrink:0 }}>
          <div>
            <div style={{ fontSize:14, fontWeight:600, color:"#e8e3da" }}>◎ Axiom — Orchestrator</div>
            <div style={{ fontSize:11, color:"#3a3530" }}>Ask anything · Auto-routes to the right agent</div>
          </div>
          {routingAgent && (
            <div style={{ display:"flex", alignItems:"center", gap:6, background:`${AGENTS[routingAgent]?.color}12`, border:`1px solid ${AGENTS[routingAgent]?.color}35`, borderRadius:7, padding:"5px 12px", animation:"msgIn 0.3s ease" }}>
              <span style={{ fontSize:12, color:AGENTS[routingAgent]?.color }}>{AGENTS[routingAgent]?.icon}</span>
              <span style={{ fontSize:11, color:AGENTS[routingAgent]?.color }}>→ {AGENTS[routingAgent]?.role}</span>
            </div>
          )}
        </div>

        {/* Messages */}
        <div style={{ flex:1, overflowY:"auto", padding:"16px 20px", display:"flex", flexDirection:"column", gap:12 }}>
          {messages.map(msg => {
            const agent = AGENTS[msg.agent||"orchestrator"];
            const color = agent?.color||"#E8D5B7";
            return (
              <div key={msg.id} className="msg-in" style={{ display:"flex", justifyContent:msg.role==="user"?"flex-end":"flex-start", gap:8 }}>
                {msg.role==="assistant" && (
                  <div style={{ width:28, height:28, borderRadius:7, background:`${color}15`, border:`1px solid ${color}35`, display:"flex", alignItems:"center", justifyContent:"center", fontSize:12, color, flexShrink:0, marginTop:16 }}>
                    {agent?.icon}
                  </div>
                )}
                <div style={{ maxWidth:"78%", minWidth:0 }}>
                  {msg.role==="assistant" && (
                    <div style={{ fontSize:9, color, letterSpacing:1.5, textTransform:"uppercase", fontWeight:700, marginBottom:4, fontFamily:"'Courier Prime',monospace" }}>{agent?.role}</div>
                  )}
                  <div style={{
                    background: msg.role==="user" ? "rgba(255,215,0,0.05)" : "rgba(255,255,255,0.03)",
                    border: `1px solid ${msg.role==="user" ? "rgba(255,215,0,0.15)" : "rgba(255,255,255,0.06)"}`,
                    borderRadius: msg.role==="user" ? "12px 12px 3px 12px" : "3px 12px 12px 12px",
                    padding:"11px 14px", fontSize:14, lineHeight:1.7, color:"#c0bbb3"
                  }}>
                    <RenderText text={msg.content} color={color} />
                  </div>
                </div>
              </div>
            );
          })}
          {loading && (
            <div style={{ display:"flex", gap:8 }}>
              <div style={{ width:28,height:28,borderRadius:7,background:"rgba(232,213,183,0.1)",border:"1px solid rgba(232,213,183,0.2)",display:"flex",alignItems:"center",justifyContent:"center",fontSize:12,color:"#E8D5B7" }}>◎</div>
              <div style={{ background:"rgba(255,255,255,0.03)",border:"1px solid rgba(255,255,255,0.06)",borderRadius:"3px 12px 12px",padding:"14px",display:"flex",gap:4,alignItems:"center",marginTop:16 }}>
                {[0,1,2].map(i=><div key={i} className="dot" style={{width:5,height:5,borderRadius:"50%",background:"#E8D5B7"}}/>)}
              </div>
            </div>
          )}
          <div ref={messagesEndRef} />
        </div>

        {/* Quick prompts */}
        <div style={{ padding:"0 20px 8px", display:"flex", gap:6, flexWrap:"wrap", flexShrink:0 }}>
          {["How does async/await work?","Help me plan learning Blender this week","Save this: vectors represent direction+magnitude","What did I study before?"].map(q=>(
            <button key={q} onClick={()=>setInput(q)} className="btn-hover" style={{ background:"rgba(255,255,255,0.03)",border:"1px solid rgba(255,255,255,0.07)",borderRadius:6,color:"#555",padding:"4px 10px",fontSize:11,fontFamily:"inherit" }}>
              {q}
            </button>
          ))}
        </div>

        {/* Input */}
        <div style={{ padding:"0 20px 16px", flexShrink:0 }}>
          <div style={{ display:"flex", gap:8, alignItems:"flex-end", background:"rgba(255,255,255,0.03)", border:"1px solid rgba(255,255,255,0.1)", borderRadius:12, padding:"7px 7px 7px 14px" }}>
            <textarea ref={inputRef} value={input} onChange={e=>setInput(e.target.value)}
              onKeyDown={e=>{if(e.key==="Enter"&&!e.shiftKey){e.preventDefault();send();}}}
              placeholder="Ask anything — earns XP every message..."
              rows={1} style={{ flex:1,background:"transparent",border:"none",color:"#d4cfc8",fontSize:14,fontFamily:"inherit",resize:"none",lineHeight:1.5,maxHeight:100,overflowY:"auto",paddingTop:4 }}
              onInput={e=>{e.target.style.height="auto";e.target.style.height=Math.min(e.target.scrollHeight,100)+"px";}}
            />
            <button onClick={send} disabled={!input.trim()||loading} style={{ background:input.trim()&&!loading?"#FFD700":"rgba(255,255,255,0.06)",border:"none",borderRadius:8,width:34,height:34,color:input.trim()&&!loading?"#080c12":"#444",fontSize:15,display:"flex",alignItems:"center",justifyContent:"center",flexShrink:0,transition:"all 0.2s",fontWeight:700 }}>↑</button>
          </div>
          <div style={{ textAlign:"center",fontSize:9,color:"#2a2520",marginTop:4,letterSpacing:1 }}>ENTER TO SEND · +12 XP PER MESSAGE</div>
        </div>
      </div>

      {/* ── RIGHT: GAME PANEL ── */}
      <div style={{ width:280, background:"#0a0e15", display:"flex", flexDirection:"column", flexShrink:0 }}>
        {/* Tab bar */}
        <div style={{ display:"flex", borderBottom:"1px solid rgba(255,255,255,0.07)", padding:"0 4px" }}>
          {[["companions","Companions"],["skills","Skills"],["achievements","Badges"],["memory","Memory"]].map(([id,label])=>(
            <button key={id} className="tab-btn" onClick={()=>setRightPanel(id)} style={{ flex:1, background:"transparent", padding:"11px 4px", fontSize:10, letterSpacing:1, textTransform:"uppercase", color:rightPanel===id?"#e8e3da":"#444", borderBottom:rightPanel===id?"2px solid #FFD700":"2px solid transparent", fontFamily:"inherit" }}>
              {label}
            </button>
          ))}
        </div>

        <div style={{ flex:1, overflowY:"auto", padding:"14px" }}>

          {/* COMPANIONS panel */}
          {rightPanel==="companions" && (
            <div>
              <div style={{ fontSize:10, color:"#444", letterSpacing:2, textTransform:"uppercase", marginBottom:12 }}>
                Your Pals · {unlockedCompanions.length}/{ALL_COMPANIONS.length} Unlocked
              </div>
              {ALL_COMPANIONS.map(c=>{
                const unlocked = c.unlockLevel <= levelData.current.level;
                return (
                  <div key={c.id} style={{ display:"flex", gap:10, padding:"10px", borderRadius:9, background:unlocked?`${c.color}08`:"rgba(255,255,255,0.02)", border:`1px solid ${unlocked?c.color+"25":"rgba(255,255,255,0.05)"}`, marginBottom:8, opacity:unlocked?1:0.4 }}>
                    <div style={{ width:36,height:36,borderRadius:8,background:unlocked?`${c.color}20`:"rgba(255,255,255,0.05)",border:`1px solid ${unlocked?c.color+"40":"rgba(255,255,255,0.1)"}`,display:"flex",alignItems:"center",justifyContent:"center",fontSize:16,color:unlocked?c.color:"#444",flexShrink:0 }}>
                      {unlocked ? c.sprite : "?"}
                    </div>
                    <div style={{ minWidth:0 }}>
                      <div style={{ fontSize:12,color:unlocked?c.color:"#444",fontWeight:600 }}>{unlocked?c.name:"???"}</div>
                      <div style={{ fontSize:10,color:"#555",marginTop:1 }}>{unlocked?c.type:`Unlocks at Level ${c.unlockLevel}`}</div>
                      {unlocked && <div style={{ fontSize:10,color:"#666",marginTop:3,lineHeight:1.4 }}>{c.desc}</div>}
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* SKILLS panel */}
          {rightPanel==="skills" && (
            <div>
              <div style={{ fontSize:10, color:"#444", letterSpacing:2, textTransform:"uppercase", marginBottom:12 }}>Skill Tree</div>
              {/* 1% better quote */}
              <div style={{ padding:"8px 10px", background:"rgba(255,215,0,0.04)", border:"1px solid rgba(255,215,0,0.12)", borderRadius:7, marginBottom:14, fontSize:10, color:"#665a30", lineHeight:1.6, fontStyle:"italic" }}>
                "1% better every day for a year = 37× better." — James Clear
              </div>
              {Object.entries(SKILL_TREE).map(([key,skill])=>{
                const level = getSkillLevel(key);
                const xp = skillXp[key]||0;
                const xpToNext = (level+1)*3;
                const xpInLevel = xp - level*3;
                const pct = level>=5?100:Math.min(100,(xpInLevel/3)*100);
                return (
                  <div key={key} style={{ marginBottom:14 }}>
                    <div style={{ display:"flex", justifyContent:"space-between", alignItems:"center", marginBottom:5 }}>
                      <div style={{ display:"flex", alignItems:"center", gap:6 }}>
                        <span style={{ color:skill.color, fontSize:13 }}>{skill.icon}</span>
                        <span style={{ fontSize:12, color:level>0?skill.color:"#666", fontWeight:level>0?600:400 }}>{skill.label}</span>
                      </div>
                      <span style={{ fontSize:10, color:level>0?skill.color:"#444", fontFamily:"'Courier Prime',monospace" }}>
                        {level>0?skill.levels[level]:"Locked"}
                      </span>
                    </div>
                    {/* Skill pip track */}
                    <div style={{ display:"flex", gap:3, marginBottom:4 }}>
                      {[0,1,2,3,4].map(i=>(
                        <div key={i} style={{ flex:1, height:4, borderRadius:2, background:i<level?skill.color:`rgba(255,255,255,0.06)`, transition:"background 0.3s" }} />
                      ))}
                    </div>
                    {level<5 && (
                      <div style={{ display:"flex", gap:3, alignItems:"center" }}>
                        <div style={{ flex:1, height:2, background:"rgba(255,255,255,0.04)", borderRadius:1, overflow:"hidden" }}>
                          <div className="skill-bar" style={{ height:"100%", width:`${pct}%`, background:skill.color, opacity:0.5 }} />
                        </div>
                        <span style={{ fontSize:9, color:"#444", fontFamily:"monospace" }}>{xpInLevel%3}/3</span>
                      </div>
                    )}
                  </div>
                );
              })}
              <div style={{ marginTop:16, padding:"8px 10px", background:"rgba(255,255,255,0.02)", borderRadius:7, border:"1px solid rgba(255,255,255,0.05)", fontSize:10, color:"#555", lineHeight:1.6 }}>
                Skills grow every time a learning session is logged. The compounding effect takes time — trust the process.
              </div>
            </div>
          )}

          {/* ACHIEVEMENTS panel */}
          {rightPanel==="achievements" && (
            <div>
              <div style={{ fontSize:10, color:"#444", letterSpacing:2, textTransform:"uppercase", marginBottom:12 }}>
                Badges · {achievements.length}/{ACHIEVEMENTS.length}
              </div>
              <div style={{ padding:"8px 10px", background:"rgba(255,215,0,0.04)", border:"1px solid rgba(255,215,0,0.12)", borderRadius:7, marginBottom:14, fontSize:10, color:"#665a30", lineHeight:1.6, fontStyle:"italic" }}>
                "Every action is a vote for your new identity." — James Clear
              </div>
              {ACHIEVEMENTS.map(a=>{
                const unlocked = achievements.includes(a.id);
                return (
                  <div key={a.id} style={{ display:"flex", gap:10, padding:"9px 10px", borderRadius:8, background:unlocked?"rgba(255,215,0,0.05)":"rgba(255,255,255,0.02)", border:`1px solid ${unlocked?"rgba(255,215,0,0.2)":"rgba(255,255,255,0.05)"}`, marginBottom:6, opacity:unlocked?1:0.5 }}>
                    <div style={{ width:30,height:30,borderRadius:7,background:unlocked?"rgba(255,215,0,0.1)":"rgba(255,255,255,0.03)",border:`1px solid ${unlocked?"rgba(255,215,0,0.3)":"rgba(255,255,255,0.08)"}`,display:"flex",alignItems:"center",justifyContent:"center",fontSize:13,color:unlocked?"#FFD700":"#444",flexShrink:0 }}>
                      {a.icon}
                    </div>
                    <div>
                      <div style={{ fontSize:12, color:unlocked?"#e8e3da":"#555", fontWeight:600 }}>{a.label}</div>
                      <div style={{ fontSize:10, color:"#555", marginTop:1 }}>{a.desc}</div>
                      <div style={{ fontSize:9, color:unlocked?"#FFD700":"#444", marginTop:2, fontFamily:"monospace" }}>+{a.xpReward} XP</div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}

          {/* MEMORY panel */}
          {rightPanel==="memory" && (
            <div>
              <div style={{ fontSize:10, color:"#444", letterSpacing:2, textTransform:"uppercase", marginBottom:12 }}>Knowledge Archive</div>
              <div style={{ padding:"8px 10px", background:"rgba(143,212,160,0.04)", border:"1px solid rgba(143,212,160,0.12)", borderRadius:7, marginBottom:14, fontSize:10, color:"#4a6650", lineHeight:1.6, fontStyle:"italic" }}>
                "Play is the most underrated productivity principle." — Ali Abdaal
              </div>
              {/* Projects */}
              {memory.projects.length>0 && <>
                <div style={{ fontSize:9, color:"#7EB8D4", letterSpacing:2, textTransform:"uppercase", marginBottom:6 }}>◷ Active Projects</div>
                {memory.projects.slice(0,3).map(p=>(
                  <div key={p.id} style={{ padding:"8px 10px",borderRadius:7,background:"rgba(126,184,212,0.05)",border:"1px solid rgba(126,184,212,0.15)",marginBottom:6 }}>
                    <div style={{ fontSize:12,color:"#7EB8D4",fontWeight:500 }}>{p.title}</div>
                    <div style={{ fontSize:10,color:"#555",marginTop:1 }}>{p.subject}</div>
                  </div>
                ))}
              </>}
              {/* Recent learning */}
              {memory.learningLog.length>0 && <>
                <div style={{ fontSize:9, color:"#8FD4A0", letterSpacing:2, textTransform:"uppercase", marginBottom:6, marginTop:10 }}>∿ Recent Sessions</div>
                {memory.learningLog.slice(0,4).map(l=>(
                  <div key={l.id} style={{ padding:"8px 10px",borderRadius:7,background:"rgba(143,212,160,0.04)",border:"1px solid rgba(143,212,160,0.12)",marginBottom:5 }}>
                    <div style={{ fontSize:11,color:"#8FD4A0",fontWeight:500 }}>{l.topic}</div>
                    <div style={{ fontSize:10,color:"#555",marginTop:1 }}>{l.subject} · {l.date}</div>
                    <div style={{ fontSize:10,color:"#777",marginTop:3,lineHeight:1.4 }}>{l.summary?.slice(0,80)}...</div>
                  </div>
                ))}
              </>}
              {/* Resources */}
              {memory.resources.length>0 && <>
                <div style={{ fontSize:9, color:"#D4A574", letterSpacing:2, textTransform:"uppercase", marginBottom:6, marginTop:10 }}>⬡ Saved Resources</div>
                {memory.resources.slice(0,3).map(r=>(
                  <div key={r.id} style={{ padding:"8px 10px",borderRadius:7,background:"rgba(212,165,116,0.04)",border:"1px solid rgba(212,165,116,0.15)",marginBottom:5 }}>
                    <div style={{ fontSize:11,color:"#D4A574",fontWeight:500 }}>{r.title}</div>
                    <div style={{ fontSize:10,color:"#777",marginTop:3,lineHeight:1.4 }}>{r.summary}</div>
                    <div style={{ display:"flex",gap:4,flexWrap:"wrap",marginTop:5 }}>
                      {r.tags?.map(t=><span key={t} style={{ fontSize:9,color:"#D4A574",background:"rgba(212,165,116,0.1)",borderRadius:4,padding:"1px 6px" }}>{t}</span>)}
                    </div>
                  </div>
                ))}
              </>}
              {memory.projects.length===0&&memory.learningLog.length===0&&memory.resources.length===0 && (
                <div style={{ textAlign:"center",padding:"40px 0",color:"#333",fontSize:12,fontStyle:"italic" }}>Your archive is empty. Start learning to fill it.</div>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
