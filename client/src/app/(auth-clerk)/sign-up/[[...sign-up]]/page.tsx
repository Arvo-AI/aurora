"use client";

import { SignUp } from "@clerk/nextjs";

export default function SignUpPage() {
  return (
    <div className="min-h-screen flex">
      <div className="flex-1 flex items-center justify-center p-8">
        <SignUp
          appearance={{
            elements: {
              rootBox: "mx-auto",
              card: "shadow-none border-0",
            },
          }}
        />
      </div>
      <div className="hidden lg:flex flex-1 items-center justify-center bg-slate-900 text-white p-12">
        <div className="max-w-md space-y-4">
          <h1 className="text-4xl font-bold leading-tight">
            Ship fast<br />with confidence.
          </h1>
          <p className="text-slate-300 text-lg">
            Aurora gives your team real-time incident intelligence, automated root cause analysis,
            and actionable remediation — so you can resolve faster and prevent recurrence.
          </p>
        </div>
      </div>
    </div>
  );
}
