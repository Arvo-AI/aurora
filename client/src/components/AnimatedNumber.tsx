"use client";

import React, { useEffect, useRef, useState, useMemo } from "react";

interface AnimatedNumberProps {
  value: number;
  format?: (n: number) => string;
  className?: string;
  duration?: number;
}

function defaultFormat(n: number): string {
  return n.toLocaleString();
}

export default function AnimatedNumber({
  value,
  format = defaultFormat,
  className = "",
  duration = 300,
}: AnimatedNumberProps) {
  const formatted = useMemo(() => format(value), [value, format]);
  const prevRef = useRef(formatted);
  const [flash, setFlash] = useState(false);

  useEffect(() => {
    if (prevRef.current !== formatted) {
      prevRef.current = formatted;
      setFlash(true);
      const t = setTimeout(() => setFlash(false), duration);
      return () => clearTimeout(t);
    }
  }, [formatted, duration]);

  return (
    <span
      className={`font-mono tabular-nums transition-colors ${
        flash ? "text-white" : ""
      } ${className}`}
      style={{ transitionDuration: `${duration}ms` }}
    >
      {formatted}
    </span>
  );
}
