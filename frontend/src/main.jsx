import React, { useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";

const API_BASE = import.meta.env.VITE_API_BASE ?? "http://127.0.0.1:8000";
const CUSTOM_EMAIL_TEMPLATE = "I am sending you the following document: …";

const statusLabels = {
  open: "Open",
  requested: "Requested",
  expected: "Expected",
  received: "Received",
};

function makeId(label) {
  return label
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

async function api(path, options) {
  const response = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!response.ok) {
    const payload = await response.json().catch(() => null);
    const detail = payload?.detail;
    const message = typeof detail === "string" ? detail : response.statusText;
    const error = new Error(message || response.statusText);
    error.status = response.status;
    error.detail = detail;
    throw error;
  }
  return response.json();
}

function App() {
  const [state, setState] = useState({
    items: [],
    fixture_emails: [],
    processed_emails: [],
    statuses: ["open", "requested", "expected", "received"],
  });
  const [customEmailText, setCustomEmailText] = useState(CUSTOM_EMAIL_TEMPLATE);
  const [queuedEmails, setQueuedEmails] = useState([]);
  const [newItemLabel, setNewItemLabel] = useState("");
  const [itemWarning, setItemWarning] = useState(null);
  const [busy, setBusy] = useState(false);
  const [checkProgress, setCheckProgress] = useState(null);
  const [statusFlashVersions, setStatusFlashVersions] = useState({});
  const [deletingItemIds, setDeletingItemIds] = useState(() => new Set());
  const [error, setError] = useState("");
  const [pendingEmailScroll, setPendingEmailScroll] = useState(null);
  const [expandedMessageIds, setExpandedMessageIds] = useState(() => new Set());
  const emailRefs = useRef(new Map());

  async function loadState() {
    setError("");
    setState(await api("/api/state"));
  }

  useEffect(() => {
    loadState().catch((err) => setError(err.message));
  }, []);

  const processedMessageIds = useMemo(
    () => new Set(state.processed_emails.map((email) => email.message_id)),
    [state.processed_emails],
  );
  const displayedEmails = useMemo(() => {
    const fixtureIds = new Set(state.fixture_emails.map((email) => email.message_id));
    const customEmails = state.processed_emails.filter(
      (email) => !fixtureIds.has(email.message_id),
    );
    return [...state.fixture_emails, ...customEmails, ...queuedEmails];
  }, [queuedEmails, state.fixture_emails, state.processed_emails]);

  useEffect(() => {
    if (!pendingEmailScroll) return;
    const email = emailRefs.current.get(pendingEmailScroll);
    if (!email) return;
    email.scrollIntoView({ behavior: "smooth", block: "center" });
    email.classList.remove("emailEvidenceHighlight");
    void email.offsetWidth;
    email.classList.add("emailEvidenceHighlight");
    const timer = window.setTimeout(() => {
      email.classList.remove("emailEvidenceHighlight");
      setPendingEmailScroll(null);
    }, 3000);
    return () => window.clearTimeout(timer);
  }, [displayedEmails, pendingEmailScroll]);

  async function runProcessAll() {
    setBusy(true);
    setError("");
    setCheckProgress({ completed: 0, total: 0 });
    try {
      const response = await fetch(`${API_BASE}/api/process-all/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          custom_email_texts: queuedEmails.map((email) => email.body),
        }),
      });
      if (!response.ok || !response.body) {
        throw new Error((await response.text()) || response.statusText);
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let streamFailed = false;
      let previousItems = state.items;

      while (true) {
        const { value, done } = await reader.read();
        buffer += decoder.decode(value, { stream: !done });
        const lines = buffer.split("\n");
        buffer = lines.pop() ?? "";

        for (const line of lines) {
          if (!line.trim()) continue;
          const event = JSON.parse(line);
          if (event.state) {
            const previousStatuses = new Map(
              previousItems.map((item) => [item.id, item.status]),
            );
            const changedItemIds = event.state.items
              .filter(
                (item) =>
                  item.status !== "open" &&
                  previousStatuses.get(item.id) !== item.status,
              )
              .map((item) => item.id);
            if (changedItemIds.length > 0) {
              setStatusFlashVersions((current) => {
                const next = { ...current };
                for (const itemId of changedItemIds) {
                  next[itemId] = (next[itemId] ?? 0) + 1;
                }
                return next;
              });
            }
            previousItems = event.state.items;
            setState(event.state);
          }
          setCheckProgress({ completed: event.completed, total: event.total });
          if (event.type === "email_error") {
            streamFailed = true;
            setError(event.error || "An email could not be processed.");
          }
        }
        if (done) break;
      }

      if (!streamFailed) setQueuedEmails([]);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
      setCheckProgress(null);
    }
  }

  async function resetDemo() {
    setBusy(true);
    setError("");
    try {
      setState(await api("/api/reset", { method: "POST", body: "{}" }));
      setQueuedEmails([]);
      setExpandedMessageIds(new Set());
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function addItem(event) {
    event.preventDefault();
    const label = newItemLabel.trim();
    const id = makeId(label);
    if (!id) return;
    setItemWarning(null);
    const exactItem = state.items.find(
      (item) => item.id === id || makeId(item.label) === id,
    );
    if (exactItem) {
      setItemWarning({ type: "exact", existingLabel: exactItem.label });
      return;
    }
    setBusy(true);
    setError("");
    try {
      const saveItem = (allowSimilar = false) =>
        api(`/api/items/${encodeURIComponent(id)}`, {
          method: "PATCH",
          body: JSON.stringify({
            label,
            status: "open",
            allow_similar: allowSimilar,
          }),
        });

      try {
        setState(await saveItem());
      } catch (err) {
        if (err.status !== 409 || err.detail?.code !== "similar_item") throw err;
        setItemWarning({
          type: "similar",
          existingLabel: err.detail.label,
          id,
          label,
        });
        return;
      }
      setNewItemLabel("");
      setItemWarning(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  async function deleteItem(item) {
    setDeletingItemIds((current) => new Set(current).add(item.id));
    setError("");
    try {
      setState(
        await api(`/api/items/${encodeURIComponent(item.id)}`, {
          method: "DELETE",
        }),
      );
    } catch (err) {
      setError(err.message);
    } finally {
      setDeletingItemIds((current) => {
        const next = new Set(current);
        next.delete(item.id);
        return next;
      });
    }
  }

  function addCustomEmail() {
    const body = customEmailText.trim();
    if (!body) return;
    const messageId = `pending-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    setQueuedEmails((current) => [
      ...current,
      {
        attachments: [],
        body,
        filename: "custom-input.txt",
        message_id: messageId,
        subject: "Custom test email",
      },
    ]);
    setCustomEmailText(CUSTOM_EMAIL_TEMPLATE);
    setExpandedMessageIds((current) => new Set(current).add(messageId));
    setPendingEmailScroll(messageId);
  }

  function showEvidenceEmail(messageId) {
    setExpandedMessageIds((current) => new Set(current).add(messageId));
    window.setTimeout(() => {
      const email = emailRefs.current.get(messageId);
      if (!email) return;
      email.scrollIntoView({ behavior: "smooth", block: "center" });
      email.classList.remove("emailEvidenceHighlight");
      void email.offsetWidth;
      email.classList.add("emailEvidenceHighlight");
      window.setTimeout(() => email.classList.remove("emailEvidenceHighlight"), 3000);
    }, 0);
  }

  function toggleEmail(messageId) {
    const willExpand = !expandedMessageIds.has(messageId);
    setExpandedMessageIds((current) => {
      const next = new Set(current);
      if (next.has(messageId)) next.delete(messageId);
      else next.add(messageId);
      return next;
    });
    if (willExpand) {
      window.setTimeout(() => {
        emailRefs.current.get(messageId)?.scrollIntoView({
          behavior: "smooth",
          block: "center",
        });
      }, 0);
    }
  }

  function handleEmailClick(messageId) {
    if (window.getSelection()?.toString().trim()) return;
    toggleEmail(messageId);
  }

  async function addSimilarItemAnyway() {
    if (!itemWarning || itemWarning.type !== "similar") return;
    setBusy(true);
    setError("");
    try {
      setState(
        await api(`/api/items/${encodeURIComponent(itemWarning.id)}`, {
          method: "PATCH",
          body: JSON.stringify({
            label: itemWarning.label,
            status: "open",
            allow_similar: true,
          }),
        }),
      );
      setNewItemLabel("");
      setItemWarning(null);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="shell">
      <header className="topbar">
        <div>
          <h1>AI Checklist Demo</h1>
        </div>
      </header>

      {error && <div className="error">{error}</div>}

      <section className="grid">
        <div className="panel fixturePanel">
          <h2>Fixture Emails</h2>
          <div className="emailList">
            <article className="email customEmailCard">
              <div className="emailHeader">
                <strong>Custom Test Email</strong>
              </div>
              <textarea
                value={customEmailText}
                onChange={(event) => setCustomEmailText(event.target.value)}
                placeholder="Paste the text of a test email..."
                disabled={busy}
              />
              <button
                className="emailProcessButton"
                onClick={addCustomEmail}
                disabled={busy || !customEmailText.trim()}
              >
                Add Email
              </button>
            </article>
            {displayedEmails.map((email) => {
              const expanded = expandedMessageIds.has(email.message_id);
              return (
              <article
                aria-expanded={expanded}
                className={`email inboxEmail${expanded ? "" : " emailCollapsed"}`}
                key={email.message_id}
                onClick={() => handleEmailClick(email.message_id)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" || event.key === " ") {
                    event.preventDefault();
                    toggleEmail(email.message_id);
                  }
                }}
                ref={(element) => {
                  if (element) emailRefs.current.set(email.message_id, element);
                  else emailRefs.current.delete(email.message_id);
                }}
                role="button"
                tabIndex={0}
              >
                {processedMessageIds.has(email.message_id) ? (
                  <span className="emailState emailStateRead" title="Processed">
                    ✓
                  </span>
                ) : (
                  <span
                    aria-label="Waiting"
                    className="emailState emailStateWaiting"
                    title="Waiting"
                  />
                )}
                <div className="emailHeader">
                  <strong>{email.subject}</strong>
                </div>
                {expanded && (
                  <>
                    <p>{email.body}</p>
                    {email.attachments.length > 0 && (
                      <small>Attachments: {email.attachments.join(", ")}</small>
                    )}
                  </>
                )}
              </article>
              );
            })}
          </div>
        </div>

        <div className="actions gridActions">
          <button className="runCheckButton" onClick={runProcessAll} disabled={busy}>
            {checkProgress && <span className="buttonSpinner" aria-hidden="true" />}
            {checkProgress && checkProgress.total > 0
              ? `${checkProgress.completed}/${checkProgress.total}`
              : checkProgress
                ? "Starting…"
                : "Run Check"}
          </button>
          <button className="secondary resetButton" onClick={resetDemo} disabled={busy}>
            Reset
          </button>
        </div>

        <div className="panel checklistPanel">
          <h2>Checklist</h2>
          <div className="checklist">
            {state.items.map((item) => (
              <ChecklistItem
                deleting={deletingItemIds.has(item.id)}
                deleteItem={deleteItem}
                item={item}
                key={item.id}
                showEvidenceEmail={showEvidenceEmail}
                statusFlashVersion={statusFlashVersions[item.id] ?? 0}
              />
            ))}
          </div>

          <form className="addItem" onSubmit={addItem}>
            <input
              placeholder="New checklist item"
              value={newItemLabel}
              onChange={(event) => {
                setNewItemLabel(event.target.value);
                setItemWarning(null);
              }}
              disabled={busy}
            />
            <button type="submit" className="secondary" disabled={busy}>
              Add
            </button>
          </form>

          {itemWarning && (
            <div className="similarItemWarning" role="alert">
              <div>
                <strong>
                  {itemWarning.type === "exact"
                    ? "This item already exists."
                    : "A similar item already exists."}
                </strong>
                <span>{itemWarning.existingLabel}</span>
              </div>
              <div className="similarItemActions">
                <button
                  className="secondary"
                  onClick={() => setItemWarning(null)}
                  type="button"
                >
                  {itemWarning.type === "exact" ? "Close" : "Cancel"}
                </button>
                {itemWarning.type === "similar" && (
                  <button onClick={addSimilarItemAnyway} type="button">
                    Add anyway
                  </button>
                )}
              </div>
            </div>
          )}

        </div>
      </section>

    </main>
  );
}

function ChecklistItem({
  deleting,
  deleteItem,
  item,
  showEvidenceEmail,
  statusFlashVersion,
}) {
  function prepareLabelScroll(event) {
    const label = event.currentTarget;
    const text = label.firstElementChild;
    const styles = window.getComputedStyle(label);
    const horizontalPadding =
      Number.parseFloat(styles.paddingLeft) + Number.parseFloat(styles.paddingRight);
    const visibleTextWidth = label.clientWidth - horizontalPadding;
    const distance = Math.max(0, text.scrollWidth - visibleTextWidth);
    label.style.setProperty("--label-scroll-distance", `-${distance}px`);
  }

  return (
    <div className="item">
      <span
        className={`itemLabel${item.evidence_message_id ? " itemLabelClickable" : ""}`}
        onClick={() => item.evidence_message_id && showEvidenceEmail(item.evidence_message_id)}
        onKeyDown={(event) => {
          if (item.evidence_message_id && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            showEvidenceEmail(item.evidence_message_id);
          }
        }}
        onMouseEnter={prepareLabelScroll}
        role={item.evidence_message_id ? "button" : undefined}
        tabIndex={item.evidence_message_id ? 0 : undefined}
        title={item.label}
      >
        <span className="itemLabelText">{item.label}</span>
      </span>
      <span className="deleteItemSlot">
        {item.deletable && (
          <button
            aria-label={`Delete ${item.label}`}
            className="deleteItemButton"
            disabled={deleting}
            onClick={() => deleteItem(item)}
            title="Delete item"
          >
            🗑
          </button>
        )}
      </span>
      <span
        aria-label={statusLabels[item.status] ?? item.status}
        className={`statusBadge status-${item.status}${item.evidence_message_id ? " statusBadgeClickable" : ""}${statusFlashVersion && item.status !== "open" ? " statusBadgeFlash" : ""}`}
        key={`${item.status}-${statusFlashVersion}`}
        onClick={() => item.evidence_message_id && showEvidenceEmail(item.evidence_message_id)}
        onKeyDown={(event) => {
          if (item.evidence_message_id && (event.key === "Enter" || event.key === " ")) {
            event.preventDefault();
            showEvidenceEmail(item.evidence_message_id);
          }
        }}
        role={item.evidence_message_id ? "button" : undefined}
        tabIndex={item.evidence_message_id ? 0 : undefined}
        title={statusLabels[item.status] ?? item.status}
      >
        {statusLabels[item.status] ?? item.status}
      </span>
    </div>
  );
}

createRoot(document.getElementById("root")).render(<App />);
