import { useState, useEffect, useCallback, useMemo } from "react";

// ─── TELEGRAM WEB APP BRIDGE ───
const tg = typeof window !== "undefined" && window.Telegram?.WebApp;
const colorScheme = tg?.colorScheme || "light";

const API = "/api";

function getInitData() {
  // Read fresh each time — Telegram may inject it after page load
  const w = typeof window !== "undefined" && window.Telegram?.WebApp;
  return w?.initData || "";
}

async function api(path, options = {}) {
  const initData = getInitData();
  const res = await fetch(`${API}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      "X-Init-Data": initData,
      ...options.headers,
    },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    console.error(`API error ${res.status}: ${text}`);
    throw new Error(`API ${res.status}`);
  }
  return res.json();
}

// ─── CONSTANTS ───
const CATEGORIES = [
  { key: "all", label: "Tutti", emoji: "📋" },
  { key: "medicine", label: "Farmaci", emoji: "💊" },
  { key: "health", label: "Salute", emoji: "🩺" },
  { key: "car", label: "Auto", emoji: "🚗" },
  { key: "house", label: "Casa", emoji: "🏠" },
  { key: "birthday", label: "Compleanni", emoji: "🎂" },
  { key: "document", label: "Documenti", emoji: "📄" },
  { key: "habit", label: "Abitudini", emoji: "💧" },
  { key: "generic", label: "Altro", emoji: "📌" },
];

const MONTHS_IT = [
  "Gennaio","Febbraio","Marzo","Aprile","Maggio","Giugno",
  "Luglio","Agosto","Settembre","Ottobre","Novembre","Dicembre"
];
const DAYS_IT = ["Lun","Mar","Mer","Gio","Ven","Sab","Dom"];

const RECURRENCE_LABELS = {
  once: "Una tantum",
  daily: "Ogni giorno",
  weekly: "Settimanale",
  monthly: "Mensile",
  every_other_day: "A giorni alterni",
};

// ─── MAIN APP ───
export default function SvampitoApp() {
  const [tab, setTab] = useState("today");
  const [dark, setDark] = useState(colorScheme === "dark");
  const [reminders, setReminders] = useState([]);
  const [stats, setStats] = useState(null);
  const [calData, setCalData] = useState({});
  const [calYear, setCalYear] = useState(new Date().getFullYear());
  const [calMonth, setCalMonth] = useState(new Date().getMonth() + 1);
  const [loading, setLoading] = useState(true);
  const [catFilter, setCatFilter] = useState("all");
  const [showCreate, setShowCreate] = useState(false);
  const [editId, setEditId] = useState(null);
  const [toast, setToast] = useState("");
  // Calendar selected day
  const [selectedDay, setSelectedDay] = useState(null);
  // Create form fields
  const [createTitle, setCreateTitle] = useState("");
  const [createDate, setCreateDate] = useState(new Date().toISOString().split("T")[0]);
  const [createTime, setCreateTime] = useState("09:00");
  const [createCategory, setCreateCategory] = useState("generic");
  const [createRecurrence, setCreateRecurrence] = useState("once");

  // Theme
  const theme = dark
    ? {
        bg: "#1a1a2e", card: "#16213e", surface: "#0f3460",
        text: "#e8e8e8", muted: "#8a8a9a", accent: "#e94560",
        accent2: "#533483", green: "#0bda51", border: "#1e3a5f",
        input: "#16213e", catBg: "#0f3460",
      }
    : {
        bg: "#faf7f2", card: "#ffffff", surface: "#f0ebe3",
        text: "#2d2d2d", muted: "#8a8a8a", accent: "#e94560",
        accent2: "#6c5ce7", green: "#00b894", border: "#e8e0d5",
        input: "#f5f0ea", catBg: "#f0ebe3",
      };

  const showToast = (msg) => {
    setToast(msg);
    setTimeout(() => setToast(""), 2500);
  };

  // ─── DATA FETCHING ───
  const loadReminders = useCallback(async (period = "all") => {
    try {
      const catParam = catFilter !== "all" ? `&category=${catFilter}` : "";
      const data = await api(`/reminders?period=${period}${catParam}`);
      setReminders(data.reminders || []);
    } catch (e) {
      console.error("Load reminders:", e);
    }
  }, [catFilter]);

  const loadStats = useCallback(async () => {
    try {
      const data = await api("/stats");
      setStats(data);
    } catch (e) {
      console.error("Load stats:", e);
    }
  }, []);

  const loadCalendar = useCallback(async () => {
    try {
      const data = await api(`/calendar/${calYear}/${calMonth}`);
      setCalData(data.days || {});
    } catch (e) {
      console.error("Load calendar:", e);
    }
  }, [calYear, calMonth]);

  useEffect(() => {
    const load = async () => {
      setLoading(true);
      if (tab === "today") await loadReminders("all");
      else if (tab === "calendar") await loadCalendar();
      else if (tab === "stats") await loadStats();
      setLoading(false);
    };
    load();
  }, [tab, loadReminders, loadCalendar, loadStats, catFilter]);

  // Tell Telegram we're ready
  useEffect(() => { tg?.ready(); tg?.expand(); }, []);

  // ─── ACTIONS ───
  const handleDone = async (id) => {
    try {
      await api(`/reminders/${id}/done`, { method: "POST" });
      showToast("✅ Fatto!");
      loadReminders("all");
      if (tab === "stats") loadStats();
    } catch (e) { showToast("❌ Errore"); }
  };

  const handleDelete = async (id) => {
    try {
      await api(`/reminders/${id}`, { method: "DELETE" });
      showToast("🗑 Cancellato");
      loadReminders("all");
    } catch (e) { showToast("❌ Errore"); }
  };

  // ─── STYLES ───
  const s = {
    app: {
      minHeight: "100vh", background: theme.bg, color: theme.text,
      fontFamily: "'DM Sans', 'Segoe UI', sans-serif",
      paddingBottom: 80, transition: "all 0.3s ease",
    },
    header: {
      padding: "16px 20px 12px", display: "flex", justifyContent: "space-between",
      alignItems: "center", borderBottom: `1px solid ${theme.border}`,
    },
    logo: { fontSize: 20, fontWeight: 700, letterSpacing: "-0.5px" },
    darkToggle: {
      background: "none", border: "none", fontSize: 20, cursor: "pointer",
      padding: 6, borderRadius: 8,
    },
    tabBar: {
      position: "fixed", bottom: 0, left: 0, right: 0,
      display: "flex", justifyContent: "space-around",
      background: theme.card, borderTop: `1px solid ${theme.border}`,
      padding: "8px 0 12px", zIndex: 100,
      boxShadow: dark ? "0 -2px 20px rgba(0,0,0,0.3)" : "0 -2px 20px rgba(0,0,0,0.05)",
    },
    tabItem: (active) => ({
      display: "flex", flexDirection: "column", alignItems: "center", gap: 2,
      background: "none", border: "none", cursor: "pointer",
      color: active ? theme.accent : theme.muted,
      fontSize: 11, fontWeight: active ? 700 : 500,
      transition: "all 0.2s ease",
    }),
    tabIcon: { fontSize: 22 },
    content: { padding: "16px 16px 0" },
    catScroll: {
      display: "flex", gap: 8, overflowX: "auto", padding: "8px 0 12px",
      scrollbarWidth: "none", msOverflowStyle: "none",
    },
    catChip: (active) => ({
      padding: "6px 14px", borderRadius: 20, whiteSpace: "nowrap",
      fontSize: 13, fontWeight: active ? 600 : 400, cursor: "pointer",
      border: active ? `2px solid ${theme.accent}` : `1px solid ${theme.border}`,
      background: active ? (dark ? theme.surface : "#fff0f3") : theme.catBg,
      color: active ? theme.accent : theme.text,
      transition: "all 0.2s ease",
      flexShrink: 0,
    }),
    reminderCard: {
      background: theme.card, borderRadius: 16, padding: "14px 16px",
      marginBottom: 10, border: `1px solid ${theme.border}`,
      display: "flex", alignItems: "center", gap: 12,
      transition: "all 0.2s ease",
    },
    emojiCircle: (cat) => ({
      width: 44, height: 44, borderRadius: 12, display: "flex",
      alignItems: "center", justifyContent: "center", fontSize: 20,
      background: dark ? theme.surface : theme.catBg, flexShrink: 0,
    }),
    cardBody: { flex: 1, minWidth: 0 },
    cardTitle: { fontSize: 15, fontWeight: 600, marginBottom: 2 },
    cardSub: { fontSize: 12, color: theme.muted },
    cardActions: { display: "flex", gap: 6, flexShrink: 0 },
    actionBtn: (color) => ({
      width: 34, height: 34, borderRadius: 10, border: "none",
      background: dark ? theme.surface : theme.catBg,
      color: color, fontSize: 16, cursor: "pointer",
      display: "flex", alignItems: "center", justifyContent: "center",
      transition: "all 0.15s ease",
    }),
    fab: {
      position: "fixed", bottom: 76, right: 20, width: 54, height: 54,
      borderRadius: "50%", background: theme.accent, color: "#fff",
      border: "none", fontSize: 26, fontWeight: 300, cursor: "pointer",
      boxShadow: "0 4px 20px rgba(233,69,96,0.4)",
      display: "flex", alignItems: "center", justifyContent: "center",
      zIndex: 99, transition: "transform 0.2s ease",
    },
    toast: {
      position: "fixed", top: 20, left: "50%", transform: "translateX(-50%)",
      background: theme.card, color: theme.text, padding: "10px 24px",
      borderRadius: 12, fontSize: 14, fontWeight: 600,
      border: `1px solid ${theme.border}`,
      boxShadow: "0 8px 30px rgba(0,0,0,0.15)",
      zIndex: 200, animation: "fadeIn 0.3s ease",
    },
    empty: {
      textAlign: "center", padding: "60px 20px", color: theme.muted, fontSize: 15,
    },
    emptyEmoji: { fontSize: 48, marginBottom: 12 },
  };

  // ─── RENDER TABS ───
  const renderToday = () => (
    <div>
      {/* Category filter */}
      <div style={s.catScroll}>
        {CATEGORIES.map((c) => (
          <button key={c.key} style={s.catChip(catFilter === c.key)}
            onClick={() => setCatFilter(c.key)}>
            {c.emoji} {c.label}
          </button>
        ))}
      </div>

      {loading ? (
        <div style={s.empty}><div style={{ fontSize: 32 }}>⏳</div>Caricamento...</div>
      ) : reminders.length === 0 ? (
        <div style={s.empty}>
          <div style={s.emptyEmoji}>🎉</div>
          Nessun reminder{catFilter !== "all" ? " in questa categoria" : ""}!
        </div>
      ) : (
        reminders.map((r) => (
          <div key={r.id} style={s.reminderCard}>
            <div style={s.emojiCircle(r.category)}>{r.emoji}</div>
            <div style={s.cardBody}>
              <div style={s.cardTitle}>{r.title}</div>
              <div style={s.cardSub}>
                {new Date(r.next_fire).toLocaleDateString("it-IT", {
                  weekday: "short", day: "numeric", month: "short",
                })}{" "}
                · {r.next_fire_time}
                {r.recurrence !== "once" && (
                  <span> · 🔁 {RECURRENCE_LABELS[r.recurrence] || r.recurrence}</span>
                )}
                {r.fire_times && <span> · ⏰ {r.fire_times.replace(/,/g, " · ")}</span>}
              </div>
            </div>
            <div style={s.cardActions}>
              <button style={s.actionBtn(theme.green)} onClick={() => handleDone(r.id)}
                title="Fatto">✓</button>
              <button style={s.actionBtn(theme.accent)} onClick={() => handleDelete(r.id)}
                title="Cancella">✕</button>
            </div>
          </div>
        ))
      )}
    </div>
  );

  const renderCalendar = () => {
    const daysInMonth = new Date(calYear, calMonth, 0).getDate();
    const firstDow = (new Date(calYear, calMonth - 1, 1).getDay() + 6) % 7; // Mon=0
    const today = new Date();
    const isCurrentMonth = today.getFullYear() === calYear && today.getMonth() + 1 === calMonth;

    const cells = [];
    for (let i = 0; i < firstDow; i++) cells.push(null);
    for (let d = 1; d <= daysInMonth; d++) cells.push(d);

    const prevMonth = () => {
      if (calMonth === 1) { setCalMonth(12); setCalYear(calYear - 1); }
      else setCalMonth(calMonth - 1);
    };
    const nextMonth = () => {
      if (calMonth === 12) { setCalMonth(1); setCalYear(calYear + 1); }
      else setCalMonth(calMonth + 1);
    };

    const dayReminders = selectedDay ? (calData[selectedDay] || []) : [];

    return (
      <div>
        {/* Month nav */}
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: "8px 0 16px" }}>
          <button onClick={prevMonth} style={{ ...s.actionBtn(theme.text), width: 40, height: 40 }}>‹</button>
          <div style={{ fontSize: 18, fontWeight: 700 }}>
            {MONTHS_IT[calMonth - 1]} {calYear}
          </div>
          <button onClick={nextMonth} style={{ ...s.actionBtn(theme.text), width: 40, height: 40 }}>›</button>
        </div>

        {/* Day headers */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 2, marginBottom: 4 }}>
          {DAYS_IT.map((d) => (
            <div key={d} style={{ textAlign: "center", fontSize: 11, fontWeight: 600, color: theme.muted, padding: 4 }}>{d}</div>
          ))}
        </div>

        {/* Calendar grid */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(7, 1fr)", gap: 2 }}>
          {cells.map((day, i) => {
            if (!day) return <div key={`e${i}`} />;
            const hasReminders = calData[day] && calData[day].length > 0;
            const isToday = isCurrentMonth && day === today.getDate();
            const isSelected = selectedDay === day;

            return (
              <button key={day} onClick={() => setSelectedDay(isSelected ? null : day)}
                style={{
                  width: "100%", aspectRatio: "1", borderRadius: 12, border: "none",
                  background: isSelected ? theme.accent : isToday ? (dark ? theme.surface : "#fff0f3") : "transparent",
                  color: isSelected ? "#fff" : isToday ? theme.accent : theme.text,
                  fontWeight: isToday || isSelected ? 700 : 400, fontSize: 14,
                  cursor: "pointer", position: "relative",
                  display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center",
                  transition: "all 0.15s ease",
                }}>
                {day}
                {hasReminders && (
                  <div style={{
                    display: "flex", gap: 2, marginTop: 2,
                  }}>
                    {calData[day].slice(0, 3).map((r, j) => (
                      <div key={j} style={{
                        width: 5, height: 5, borderRadius: "50%",
                        background: isSelected ? "#fff" : theme.accent,
                      }} />
                    ))}
                  </div>
                )}
              </button>
            );
          })}
        </div>

        {/* Selected day details */}
        {selectedDay && (
          <div style={{ marginTop: 16 }}>
            <div style={{ fontSize: 14, fontWeight: 700, marginBottom: 10, color: theme.muted }}>
              {selectedDay} {MONTHS_IT[calMonth - 1]}
            </div>
            {dayReminders.length === 0 ? (
              <div style={{ ...s.empty, padding: 30 }}>Nessun reminder</div>
            ) : (
              dayReminders.map((r) => (
                <div key={r.id} style={s.reminderCard}>
                  <div style={s.emojiCircle(r.category)}>{r.emoji}</div>
                  <div style={s.cardBody}>
                    <div style={s.cardTitle}>{r.title}</div>
                    <div style={s.cardSub}>{r.next_fire_time}</div>
                  </div>
                  <div style={s.cardActions}>
                    <button style={s.actionBtn(theme.green)} onClick={() => handleDone(r.id)}>✓</button>
                    <button style={s.actionBtn(theme.accent)} onClick={() => handleDelete(r.id)}>✕</button>
                  </div>
                </div>
              ))
            )}
          </div>
        )}
      </div>
    );
  };

  const renderStats = () => {
    if (!stats) return <div style={s.empty}><div style={{ fontSize: 32 }}>⏳</div>Caricamento...</div>;

    const statCards = [
      { label: "Attivi", value: stats.total_active, emoji: "📋", color: theme.accent2 },
      { label: "Oggi", value: stats.completed_today, emoji: "✅", color: theme.green },
      { label: "Settimana", value: stats.completed_week, emoji: "📊", color: theme.accent },
      { label: "Streak", value: `${stats.streak_days}d`, emoji: "🔥", color: "#ff9f43" },
    ];

    const catEntries = Object.entries(stats.by_category || {}).sort((a, b) => b[1] - a[1]);
    const totalCat = catEntries.reduce((s, [, v]) => s + v, 0) || 1;

    return (
      <div>
        {/* Stat cards */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 10, marginBottom: 20 }}>
          {statCards.map((sc) => (
            <div key={sc.label} style={{
              background: theme.card, borderRadius: 16, padding: "18px 16px",
              border: `1px solid ${theme.border}`, textAlign: "center",
            }}>
              <div style={{ fontSize: 28, marginBottom: 4 }}>{sc.emoji}</div>
              <div style={{ fontSize: 28, fontWeight: 800, color: sc.color }}>{sc.value}</div>
              <div style={{ fontSize: 12, color: theme.muted, marginTop: 2 }}>{sc.label}</div>
            </div>
          ))}
        </div>

        {/* Completion rate */}
        <div style={{
          background: theme.card, borderRadius: 16, padding: 20,
          border: `1px solid ${theme.border}`, marginBottom: 16,
        }}>
          <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 10 }}>
            <span style={{ fontSize: 14, fontWeight: 600 }}>Tasso completamento</span>
            <span style={{ fontSize: 14, fontWeight: 700, color: theme.green }}>
              {stats.completion_rate_week}%
            </span>
          </div>
          <div style={{
            height: 10, borderRadius: 5,
            background: dark ? theme.surface : theme.catBg, overflow: "hidden",
          }}>
            <div style={{
              height: "100%", borderRadius: 5, background: theme.green,
              width: `${Math.min(stats.completion_rate_week, 100)}%`,
              transition: "width 0.8s ease",
            }} />
          </div>
        </div>

        {/* By category */}
        {catEntries.length > 0 && (
          <div style={{
            background: theme.card, borderRadius: 16, padding: 20,
            border: `1px solid ${theme.border}`,
          }}>
            <div style={{ fontSize: 14, fontWeight: 600, marginBottom: 14 }}>Per categoria</div>
            {catEntries.map(([cat, count]) => {
              const catInfo = CATEGORIES.find((c) => c.key === cat) || { emoji: "📌", label: cat };
              const pct = Math.round((count / totalCat) * 100);
              return (
                <div key={cat} style={{ marginBottom: 12 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 4 }}>
                    <span>{catInfo.emoji} {catInfo.label}</span>
                    <span style={{ color: theme.muted }}>{count} ({pct}%)</span>
                  </div>
                  <div style={{ height: 6, borderRadius: 3, background: dark ? theme.surface : theme.catBg }}>
                    <div style={{
                      height: "100%", borderRadius: 3, background: theme.accent,
                      width: `${pct}%`, transition: "width 0.5s ease",
                    }} />
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  };

  // ─── CREATE MODAL ───
  const renderCreateModal = () => {
    const handleSubmit = async () => {
      if (!createTitle.trim()) return;
      try {
        await api("/reminders", {
          method: "POST",
          body: JSON.stringify({ title: createTitle.trim(), date: createDate, time: createTime, category: createCategory, recurrence: createRecurrence }),
        });
        showToast("✅ Creato!");
        setShowCreate(false);
        setCreateTitle("");
        setCreateDate(new Date().toISOString().split("T")[0]);
        setCreateTime("09:00");
        setCreateCategory("generic");
        setCreateRecurrence("once");
        loadReminders("all");
      } catch (e) { showToast("❌ Errore nella creazione"); }
    };

    return (
      <div style={{
        position: "fixed", inset: 0, background: "rgba(0,0,0,0.5)",
        zIndex: 300, display: "flex", alignItems: "flex-end",
        animation: "fadeIn 0.2s ease",
      }} onClick={() => setShowCreate(false)}>
        <div style={{
          background: theme.card, borderRadius: "24px 24px 0 0",
          padding: "24px 20px 32px", width: "100%",
          maxHeight: "85vh", overflow: "auto",
        }} onClick={(e) => e.stopPropagation()}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 20 }}>
            <h3 style={{ margin: 0, fontSize: 18, fontWeight: 700 }}>Nuovo reminder</h3>
            <button onClick={() => setShowCreate(false)}
              style={{ background: "none", border: "none", fontSize: 24, color: theme.muted, cursor: "pointer" }}>✕</button>
          </div>

          {/* Title */}
          <input value={createTitle} onChange={(e) => setCreateTitle(e.target.value)}
            placeholder="Cosa devi ricordare?"
            style={{
              width: "100%", padding: "14px 16px", borderRadius: 14, fontSize: 16,
              border: `2px solid ${theme.border}`, background: theme.input,
              color: theme.text, outline: "none", marginBottom: 14,
              boxSizing: "border-box",
            }}
          />

          {/* Date & Time */}
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10, marginBottom: 14 }}>
            <input type="date" value={createDate} onChange={(e) => setCreateDate(e.target.value)}
              style={{
                padding: "12px 14px", borderRadius: 14, fontSize: 14,
                border: `2px solid ${theme.border}`, background: theme.input,
                color: theme.text, outline: "none",
              }}
            />
            <input type="time" value={createTime} onChange={(e) => setCreateTime(e.target.value)}
              style={{
                padding: "12px 14px", borderRadius: 14, fontSize: 14,
                border: `2px solid ${theme.border}`, background: theme.input,
                color: theme.text, outline: "none",
              }}
            />
          </div>

          {/* Category */}
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8, color: theme.muted }}>Categoria</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 14 }}>
            {CATEGORIES.filter((c) => c.key !== "all").map((c) => (
              <button key={c.key} onClick={() => setCreateCategory(c.key)}
                style={{
                  ...s.catChip(createCategory === c.key),
                  fontSize: 12, padding: "5px 10px",
                }}>
                {c.emoji} {c.label}
              </button>
            ))}
          </div>

          {/* Recurrence */}
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 8, color: theme.muted }}>Ricorrenza</div>
          <div style={{ display: "flex", flexWrap: "wrap", gap: 6, marginBottom: 20 }}>
            {Object.entries(RECURRENCE_LABELS).map(([key, label]) => (
              <button key={key} onClick={() => setCreateRecurrence(key)}
                style={{
                  ...s.catChip(createRecurrence === key),
                  fontSize: 12, padding: "5px 10px",
                }}>
                {label}
              </button>
            ))}
          </div>

          {/* Submit */}
          <button onClick={handleSubmit}
            style={{
              width: "100%", padding: 16, borderRadius: 16, border: "none",
              background: theme.accent, color: "#fff", fontSize: 16,
              fontWeight: 700, cursor: "pointer",
              opacity: createTitle.trim() ? 1 : 0.5,
            }}>
            Salva reminder
          </button>
        </div>
      </div>
    );
  };

  // ─── LAYOUT ───
  return (
    <div style={s.app}>
      <style>{`
        @import url('https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700;800&display=swap');
        * { box-sizing: border-box; margin: 0; padding: 0; }
        ::-webkit-scrollbar { display: none; }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }
        input[type="date"], input[type="time"] { color-scheme: ${dark ? "dark" : "light"}; }
      `}</style>

      {/* Header */}
      <div style={s.header}>
        <div style={s.logo}>🧠 Svampito</div>
        <button style={s.darkToggle} onClick={() => setDark(!dark)}>
          {dark ? "☀️" : "🌙"}
        </button>
      </div>

      {/* Content */}
      <div style={s.content}>
        {tab === "today" && renderToday()}
        {tab === "calendar" && renderCalendar()}
        {tab === "stats" && renderStats()}
      </div>

      {/* FAB */}
      {tab !== "stats" && (
        <button style={s.fab} onClick={() => setShowCreate(true)}>+</button>
      )}

      {/* Create modal */}
      {showCreate && renderCreateModal()}

      {/* Toast */}
      {toast && <div style={s.toast}>{toast}</div>}

      {/* Tab bar */}
      <div style={s.tabBar}>
        {[
          { key: "today", icon: "📋", label: "Reminder" },
          { key: "calendar", icon: "📅", label: "Calendario" },
          { key: "stats", icon: "📊", label: "Statistiche" },
        ].map((t) => (
          <button key={t.key} style={s.tabItem(tab === t.key)}
            onClick={() => setTab(t.key)}>
            <span style={s.tabIcon}>{t.icon}</span>
            {t.label}
          </button>
        ))}
      </div>
    </div>
  );
}
