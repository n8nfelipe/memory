// Auto-capture Opencode sessions into the local llm-memory journal.
//
// Listens to session.created / session.idle / session.deleted and shells out
// to `memory capture-opencode --input <json>`. Only user/assistant text is
// stored; tool calls and tool results are ignored to keep token usage minimal.
// Hooks never throw: capture failures must not block the conversation.
export const LlmMemoryAuto = async ({ client, $, directory }) => {
  const runCapture = async (payload) => {
    const tmp =
      `${process.env.TMPDIR || "/tmp"}/opencode-memory-` +
      `${Date.now()}-${Math.random().toString(36).slice(2)}.json`;
    try {
      await Bun.write(tmp, JSON.stringify(payload));
      // Output is discarded; `memory export` / `memory_search` show the result.
      await $`memory capture-opencode --input ${tmp} > /dev/null 2>&1`;
    } catch {
      // Never block the session on capture failures.
    } finally {
      try {
        await Bun.file(tmp).unlink();
      } catch {
        // Temp file cleanup is best-effort.
      }
    }
  };

  const transcript = async (sessionID) => {
    try {
      const res = await client.session.messages({
        path: { id: sessionID },
        query: { directory },
      });
      const rows = Array.isArray(res?.data) ? res.data : Array.isArray(res) ? res : [];
      const messages = [];
      for (const row of rows) {
        const info = row?.info;
        if (!info || (info.role !== "user" && info.role !== "assistant")) continue;
        const texts = (row?.parts || [])
          .filter(
            (part) =>
              part?.type === "text" &&
              !part?.ignored &&
              typeof part?.text === "string" &&
              part.text.trim(),
          )
          .map((part) => part.text);
        if (!texts.length || typeof info.id !== "string") continue;
        messages.push({
          id: info.id,
          role: info.role,
          content: texts.join("\n").slice(0, 40000),
        });
      }
      return messages;
    } catch {
      return null;
    }
  };

  return {
    event: async ({ event }) => {
      try {
        if (event.type === "session.created") {
          const info = event.properties?.info;
          if (!info?.id) return;
          await runCapture({
            opencode_session_id: info.id,
            hook: "session.created",
            cwd: directory,
            title: typeof info.title === "string" ? info.title : "",
          });
        } else if (event.type === "session.idle") {
          const sessionID = event.properties?.sessionID;
          if (!sessionID) return;
          const messages = await transcript(sessionID);
          await runCapture({
            opencode_session_id: sessionID,
            hook: "session.idle",
            cwd: directory,
            messages: messages || [],
          });
        } else if (event.type === "session.deleted") {
          const info = event.properties?.info;
          const sessionID = event.properties?.sessionID ?? info?.id;
          if (!sessionID) return;
          await runCapture({
            opencode_session_id: sessionID,
            hook: "session.deleted",
            cwd: directory,
          });
        }
      } catch {
        // Never block the session on capture failures.
      }
    },
  };
};
