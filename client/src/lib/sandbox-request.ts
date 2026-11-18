/** Bound anonymous request bodies before the shared proxy buffers them. */
import { challengeQuestions } from "./sandbox-questions";
export class SandboxRequestError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

export async function readSandboxRequest(request: Request, timeoutMs = 10_000, kind: "investigate" | "challenge" = "investigate"): Promise<string> {
  const limit = kind === "challenge" ? 65536 : 512;
  const contentType = request.headers.get("content-type")?.split(";")[0].trim().toLowerCase();
  if (contentType !== "application/json") throw new SandboxRequestError("Send a JSON object with a case_id.", 415);
  if (Number(request.headers.get("content-length")) > limit) throw new SandboxRequestError("Request is too large.", 413);
  if (!request.body) throw new SandboxRequestError("Choose case-a, case-b or case-c.", 400);

  const reader = request.body.getReader();
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    const read = async () => {
      const chunks: Uint8Array[] = [];
      let size = 0;
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        size += value.byteLength;
        if (size > limit) throw new SandboxRequestError("Request is too large.", 413);
        chunks.push(value);
      }
      const bytes = new Uint8Array(size);
      let offset = 0;
      for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
      let payload;
      try { payload = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes)); }
      catch { throw new SandboxRequestError("Send valid JSON with a case_id.", 400); }
      if (kind === "challenge") {
        if (!payload || Array.isArray(payload) || Object.keys(payload).length !== 2
          || !challengeQuestions.some(question => question.id === payload.question_id)
          || typeof payload.run_token !== "string" || payload.run_token.length > 60000
          || !/^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]{43}$/.test(payload.run_token)) {
          throw new SandboxRequestError("Choose a preset question for a completed investigation. No other fields are accepted.", 400);
        }
        return JSON.stringify({ run_token: payload.run_token, question_id: payload.question_id });
      }
      if (!payload || Array.isArray(payload) || Object.keys(payload).length !== 1
        || !["case-a", "case-b", "case-c"].includes(payload.case_id)) {
        throw new SandboxRequestError("Choose case-a, case-b or case-c. No other fields are accepted.", 400);
      }
      return JSON.stringify({ case_id: payload.case_id });
    };
    return await Promise.race([
      read(),
      new Promise<never>((_, reject) => {
        timer = setTimeout(() => reject(new SandboxRequestError("Request upload timed out. Please retry.", 408)), timeoutMs);
      }),
    ]);
  } finally {
    clearTimeout(timer);
    void reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}
