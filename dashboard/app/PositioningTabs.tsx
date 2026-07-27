"use client";

import { useState } from "react";
import type { DecisionReport, Signal } from "../lib/decision-report";

type PositioningItems = DecisionReport["marketPositioning"]["items"];

function SignalMark({ signal }: { signal: Signal }) {
  return <span className={`signal-mark signal-${signal}`} aria-hidden="true" />;
}

export function PositioningTabs({ items }: { items: PositioningItems }) {
  const [activeTab, setActiveTab] = useState(items[0]?.id ?? "");

  return (
    <div className="report-tabs positioning-tabs">
      <div className="tab-list" role="tablist" aria-label="미국 주식 포지셔닝 보기">
        {items.map((item) => (
          <button
            aria-controls={`positioning-${item.id}-panel`}
            aria-selected={activeTab === item.id}
            className={activeTab === item.id ? "active" : ""}
            id={`positioning-${item.id}-tab`}
            key={item.id}
            onClick={() => setActiveTab(item.id)}
            role="tab"
            type="button"
          >
            <span>{item.label}</span>
            <small>{item.status}</small>
          </button>
        ))}
      </div>

      {items.map((item) => (
        <section
          aria-labelledby={`positioning-${item.id}-tab`}
          className="tab-panel"
          hidden={activeTab !== item.id}
          id={`positioning-${item.id}-panel`}
          key={item.id}
          role="tabpanel"
        >
          <article className={`positioning-card positioning-${item.signal}`}>
            <div className="positioning-card-head">
              <div>
                <SignalMark signal={item.signal} />
                <span>{item.label}</span>
              </div>
              <strong>{item.status}</strong>
            </div>
            <div className="positioning-value">
              <strong>{item.value}</strong>
              <span>{item.comparison}</span>
            </div>
            <p className="positioning-asof">{item.asOf}</p>
            <p className="positioning-read">{item.interpretation}</p>
            <p className="positioning-limit">{item.limitation}</p>
          </article>
        </section>
      ))}
    </div>
  );
}
