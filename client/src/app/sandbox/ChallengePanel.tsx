"use client";

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { Loader2, MessageCircle, RotateCcw } from "lucide-react";
import { challengeQuestions } from "../../lib/sandbox-questions";
import type { QuestionId } from "../../lib/sandbox-questions";
import { parseChallenge, readSandboxResponse, SandboxResponseError } from "./contract";
import type { ChallengeResult, Finding, Investigation, SampleCase } from "./contract";
import styles from "./sandbox.module.css";

type AnswerState = { status: "running" | "complete" | "error"; result?: ChallengeResult; error?: string; retryable?: boolean };

export default function ChallengePanel({ sample, investigation, citations }: {
  sample: SampleCase;
  investigation: Investigation;
  citations: (finding: Finding) => ReactNode;
}) {
  const [selected, setSelected] = useState<QuestionId | null>(null);
  const [answers, setAnswers] = useState<Partial<Record<QuestionId, AnswerState>>>({});
  const active = useRef<AbortController | null>(null);
  const cache = useRef(new Map<QuestionId, ChallengeResult>());
  const busy = Object.values(answers).some(answer => answer?.status === "running");
  const answer = selected ? answers[selected] : undefined;

  useEffect(() => () => {
    active.current?.abort();
    active.current = null;
  }, []);

  async function ask(questionId: QuestionId) {
    if (active.current || !investigation.run_token) return;
    setSelected(questionId);
    const saved = cache.current.get(questionId);
    if (saved) {
      setAnswers(previous => ({ ...previous, [questionId]: { status: "complete", result: saved } }));
      return;
    }
    const controller = new AbortController();
    active.current = controller;
    const timeout = setTimeout(() => controller.abort("timeout"), 75_000);
    setAnswers(previous => ({ ...previous, [questionId]: { status: "running" } }));
    try {
      const response = await fetch("/api/sandbox/challenge", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ run_token: investigation.run_token, question_id: questionId }), signal: controller.signal,
      });
      const result = parseChallenge(await readSandboxResponse(response), sample, investigation, questionId);
      if (active.current !== controller) return;
      cache.current.set(questionId, result);
      setAnswers(previous => ({ ...previous, [questionId]: { status: "complete", result } }));
    } catch (error) {
      if (active.current !== controller || (controller.signal.aborted && controller.signal.reason !== "timeout")) return;
      setAnswers(previous => ({ ...previous, [questionId]: { status: "error", retryable: !(error instanceof SandboxResponseError && [400, 410].includes(error.status)), error: controller.signal.reason === "timeout"
        ? "This challenge reached its time limit. You can retry this question."
        : error instanceof Error ? error.message : "The challenge could not finish. Please retry." } }));
    } finally {
      clearTimeout(timeout);
      if (active.current === controller) active.current = null;
    }
  }

  return <section className={styles.probing} aria-label={`Challenge ${sample.label}'s diagnosis`} aria-busy={busy}>
    <h4><MessageCircle size={15} /> Challenge Aurora&apos;s diagnosis</h4>
    <p className={styles.probeIntro}>Choose a question. Check Aurora&apos;s answer against the evidence.</p>
    {!investigation.run_token && <p className={styles.probeIntro}>Run a new comparison to unlock challenges for this diagnosis.</p>}
    <div className={styles.probeQuestions}>{challengeQuestions.map(question => <button
      key={question.id} disabled={busy || !investigation.run_token} aria-pressed={selected === question.id}
      onClick={() => void ask(question.id)}
    >{question.label}{answers[question.id]?.status === "complete" && <span className={styles.answeredLabel}>Answered</span>}</button>)}</div>
    <div aria-live="polite" aria-atomic="false">
      {answer?.status === "running" && <p className={styles.probeLoading}><Loader2 size={14} className={styles.spin} /> Aurora is checking the evidence...</p>}
      {answer?.status === "error" && <div className={styles.probeError} role="alert"><p>{answer.error}</p>{answer.retryable
        ? <button className={styles.retryButton} onClick={() => selected && void ask(selected)}><RotateCcw size={13} /> Retry this question</button>
        : <a className={styles.sourceLink} href="#run-comparison">Back to comparison controls</a>}</div>}
      {answer?.result && <div className={styles.probeAnswer}>
        <span className={styles.eyebrow}>AURORA&apos;S RESPONSE</span>
        <p className={styles.probeDirect}>{answer.result.response.answer}</p>
        <div className={styles.probeSupport}>{answer.result.response.supporting_evidence.map((finding, index) => <div key={index}><p>{finding.explanation}</p>{citations(finding)}</div>)}</div>
        <p><strong>Remaining uncertainty:</strong> {answer.result.response.uncertainty}</p>
        <p><strong>Check next:</strong> {answer.result.response.next_check}</p>
        <p className={styles.runMeta}>{(answer.result.elapsed_ms / 1000).toFixed(1)}s · {answer.result.retrieved_evidence_ids.length} records read · Live response</p>
        <p className={styles.probeNote}>Completed answers stay available in this comparison. Selecting one again uses the saved response.</p>
      </div>}
    </div>
  </section>;
}
