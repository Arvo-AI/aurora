export const challengeQuestions = [
  { id: "alternatives", label: "Could something else explain this error?" },
  { id: "evidence", label: "Which evidence makes the difference?" },
  { id: "falsify", label: "What would prove your diagnosis wrong?" },
] as const;

export type QuestionId = typeof challengeQuestions[number]["id"];
