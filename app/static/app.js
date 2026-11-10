"use strict";

const $ = (id) => document.getElementById(id);
const money = (pence) => new Intl.NumberFormat("en-GB", {
  style: "currency", currency: "GBP", maximumFractionDigits: 2
}).format(pence / 100);
const dateTime = (iso) => new Date(iso).toLocaleString("en-GB", {
  dateStyle: "short", timeStyle: "short"
});
const generatedKey = () => (crypto.randomUUID?.() || `${Date.now()}-${Math.random()}`);
const state = { accounts: [], lastPayload: null, selectedPaymentId: null, lastStatus: null };

async function getJson(path, options = {}) {
  const response = await fetch(path, options);
  const json = await response.json();
  if (!response.ok) {
    const msg = typeof json.detail === "string" ? json.detail : "Unable to complete request";
    throw new Error(msg);
  }
  return json;
}

function tag(text, extraClass = "") {
  const s = document.createElement("span");
  s.className = extraClass;
  s.textContent = text;
  return s;
}

function drawAccountOptions(accounts) {
  state.accounts = accounts;
  const oldFrom = $("sender").value;
  const oldTo = $("recipient").value;
  for (const selectId of ["sender", "recipient"]) {
    const dropdown = $(selectId);
    dropdown.replaceChildren();
    for (const account of accounts) {
      const option = document.createElement("option");
      option.value = account.id;
      option.textContent = account.name;
      dropdown.append(option);
    }
  }
  $("sender").value = accounts.some(a => a.id === oldFrom) ? oldFrom : accounts[0]?.id || "";
  $("recipient").value = accounts.some(a => a.id === oldTo) ? oldTo : accounts[1]?.id || "";
}

async function refreshSelectedBalance() {
  const selected = $("sender").value;
  if (!selected) return;
  const current = await getJson(`/api/demo/balance/${encodeURIComponent(selected)}`);
  // A single selected sandbox identity, never the recipient's account funds.
  if ($("sender").value === selected) {
    $("selected-account-name").textContent = current.name;
    $("selected-balance").textContent = money(current.balance_pence);
  }
}

function drawStats(stats) {
  $("metric-total").textContent = stats.total;
  $("metric-volume").textContent = money(stats.volume_pence);
  $("metric-flagged").textContent = stats.flagged;
  $("metric-rate").textContent = `${stats.total ? Math.round((stats.approved / stats.total) * 100) : 0}%`;
  $("donut-value").textContent = stats.total;
  for (const status of ["approved","flagged","declined","pending"]) {
    $(`legend-${status}`).textContent = stats[status];
  }
  const base = Math.max(1, stats.total);
  const a = stats.approved / base * 100;
  const b = a + stats.flagged / base * 100;
  const c = b + stats.declined / base * 100;
  $("donut").style.background = `conic-gradient(#42a783 0 ${a}%, #e7af5a ${a}% ${b}%, #e78179 ${b}% ${c}%, #7d91a0 ${c}% 100%)`;
}

function drawPayments(items, accounts) {
  const names = new Map(accounts.map(a => [a.id, a.name]));
  const body = $("activity-body");
  body.replaceChildren();
  if (!items.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = 7;
    td.textContent = "No payments yet. Queue a simulated transfer to begin.";
    tr.append(td); body.append(tr); return;
  }
  for (const item of items) {
    const tr = document.createElement("tr");
    const cells = [item.id, `${names.get(item.sender_id)} → ${names.get(item.recipient_id)}`,
      money(item.amount_pence), `${item.risk_score}/100`, item.status, dateTime(item.created_at)];
    cells.forEach((value, index) => {
      const td = document.createElement("td");
      if (index === 0) td.className = "payment-id";
      if (index === 2) td.className = "amount-cell";
      if (index === 4) {
        const s = tag(value, `status-badge ${value}`);
        td.append(s);
      } else {
        td.textContent = value;
      }
      tr.append(td);
    });
    const buttonCell = document.createElement("td");
    const button = document.createElement("button");
    button.className = "micro-button";
    button.textContent = "Inspect";
    button.addEventListener("click", () => {
      state.selectedPaymentId = item.id;
      inspect(item.id).catch(showError);
      $("ledger").scrollIntoView({behavior: "smooth"});
    });
    buttonCell.append(button); tr.append(buttonCell); body.append(tr);
  }
}

function drawReviews(items, accounts) {
  const names = new Map(accounts.map(a => [a.id, a.name]));
  const list = $("review-list");
  list.replaceChildren();
  if (!items.length) {
    const p = tag("✓ No flagged payments awaiting manual review.", "empty-review");
    list.append(p); return;
  }
  for (const item of items) {
    const section = document.createElement("div"); section.className = "review-card";
    const info = document.createElement("div"); info.className = "review-info";
    const title = tag(`${money(item.amount_pence)} · ${names.get(item.sender_id)} → ${names.get(item.recipient_id)}`, "review-title");
    const details = tag(`${item.id} · Risk ${item.risk_score}/100 · ${item.risk_reasons.join("; ")}`, "review-subtitle");
    info.append(title, details);
    const controls = document.createElement("div"); controls.className = "review-controls";
    const note = document.createElement("input");
    note.type = "text"; note.placeholder = "Reason for analyst decision"; note.maxLength = 200;
    note.setAttribute("aria-label", `Review note for ${item.id}`);
    const approve = document.createElement("button"); approve.className = "micro-button approve-btn"; approve.textContent = "Approve";
    const reject = document.createElement("button"); reject.className = "micro-button reject-btn"; reject.textContent = "Reject";
    for (const [button, decision] of [[approve, "approve"], [reject, "reject"]]) {
      button.addEventListener("click", async () => {
        if (note.value.trim().length < 2) { note.focus(); note.placeholder = "Enter a short reason first"; return; }
        approve.disabled = reject.disabled = true;
        try {
          const result = await getJson(`/api/reviews/${item.id}`, {
            method: "POST", headers: {"Content-Type":"application/json"},
            body: JSON.stringify({decision, note: note.value.trim()})
          });
          displayResult(`Reviewed ${item.id}: ${result.status}. ${result.risk_reasons.slice(-1)[0]}`,result.status);
          state.selectedPaymentId = item.id;
          await refresh();
          await inspect(item.id);
        } catch (error) {
          displayResult(error.message, "error");
          approve.disabled = reject.disabled = false;
        }
      });
    }
    controls.append(note, approve, reject);
    section.append(info, controls);
    list.append(section);
  }
}

function drawEvents(items) {
  const list = $("event-list"); list.replaceChildren();
  for (const item of items.slice(0, 10)) {
    const event = document.createElement("div"); event.className = "event-item";
    event.append(tag(item.event_type, "event-type"), tag(item.delivery_status.replace("_simulated", ""), "event-status"));
    event.append(tag(`${item.payment_id} · ${dateTime(item.created_at)}`,"event-sub"));
    list.append(event);
  }
}

function drawReconciliation(data) {
  const el = $("reconcile-status");
  el.className = `reconcile-status ${data.ok ? "reconciled" : "unbalanced"}`;
  el.textContent = data.ok
    ? `✓ Balanced: ledger entries match account balances · ${money(data.current_total_pence)} simulated funds` 
    : `⚠ Reconciliation failed: ${data.account_mismatches.length} account mismatches, ${data.imbalanced_transfers.length} imbalanced payments`;
}

async function inspect(paymentId) {
  const [payment, ledger, audit] = await Promise.all([
    getJson(`/api/payments/${paymentId}`),
    getJson(`/api/payments/${paymentId}/ledger`),
    getJson(`/api/payments/${paymentId}/audit`)
  ]);
  const container = $("inspection-list"); container.replaceChildren();
  container.append(tag(`${payment.id} · ${payment.status.toUpperCase()} · ${money(payment.amount_pence)}`,"inspect-heading"));
  container.append(tag(`Risk ${payment.risk_score}/100 · ${payment.risk_reasons.join("; ")}`,"inspect-description"));
  container.append(tag("Ledger entries", "inspect-label"));
  if (!ledger.length) {
    container.append(tag("No money moved for this payment.","inspect-description"));
  } else {
    for (const entry of ledger) {
      const row = document.createElement("div"); row.className = "ledger-row";
      row.append(tag(entry.account_id),tag(`${entry.delta_pence > 0 ? "+" : ""}${money(entry.delta_pence)}`));
      container.append(row);
    }
  }
  container.append(tag("Audit history", "inspect-label"));
  for (const e of audit) {
    container.append(tag(`${dateTime(e.created_at)} · ${e.event_type}`,"audit-line"));
  }
}

function displayResult(message, kind) {
  const el = $("payment-result");
  el.hidden = false;
  el.className = `payment-result ${kind}`;
  el.textContent = message;
}
function showError(error) { displayResult(`Request failed: ${error.message}`, "error"); }

async function refresh() {
  const [accounts, payments, stats, reviews, events, audit] = await Promise.all([
    getJson("/api/accounts"),getJson("/api/payments"),getJson("/api/stats"),
    getJson("/api/reviews"),getJson("/api/events"),getJson("/api/reconciliation")
  ]);
  drawAccountOptions(accounts);
  drawStats(stats); drawPayments(payments, accounts); drawReviews(reviews, accounts);
  drawEvents(events); drawReconciliation(audit);
  await refreshSelectedBalance();
}

async function submitPayment(payload) {
  $("pay-button").disabled = true;
  try {
    const result = await getJson("/api/payments", {
      method: "POST",headers: {"Content-Type":"application/json"},body: JSON.stringify(payload)
    });
    displayResult(`${result.replayed ? "Idempotent retry" : "Queued"}: ${result.id}\nCurrent status: ${result.status}\nThe background worker will evaluate the transfer.`, "pending");
    state.selectedPaymentId = result.id;
    state.lastStatus = null;
    await refresh();
    // Periodic refresh will show final state. No duplicate request is made.
  } catch(error) { showError(error); }
  finally { $("pay-button").disabled = false; }
}

$("payment-form").addEventListener("submit", async event => {
  event.preventDefault();
  const data = {sender_id: $("sender").value,recipient_id: $("recipient").value,
    amount: $("amount").value,note: $("note").value.trim(),idempotency_key: generatedKey()};
  state.lastPayload = data;
  $("retry-button").disabled = false;
  await submitPayment(data);
});
$("retry-button").addEventListener("click", async () => {
  if (state.lastPayload) await submitPayment(state.lastPayload);
});
$("sender").addEventListener("change", () => refreshSelectedBalance().catch(showError));
$("refresh-button").addEventListener("click", () => refresh().catch(showError));

refresh().catch(showError);
setInterval(async () => {
  try {
    if (!document.hidden) {
      await refresh();
      if (state.selectedPaymentId) {
        const item = await getJson(`/api/payments/${state.selectedPaymentId}`);
        if (item.status !== state.lastStatus && item.status !== "pending") {
          displayResult(`Payment ${item.status}: ${item.id}\nRisk ${item.risk_score}/100\n${item.risk_reasons.join("; ")}`,item.status);
          state.lastStatus = item.status;
        }
      }
    }
  } catch(error) { console.warn("Dashboard refresh failed",error); }
}, 1600);
