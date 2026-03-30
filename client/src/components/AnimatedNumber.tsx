"use client";

import React, { useEffect, useRef, useState, useMemo } from "react";

interface AnimatedNumberProps {
  value: number;
  format?: (n: number) => string;
  className?: string;
  /** Duration in ms for the digit roll animation */
  duration?: number;
}

function defaultFormat(n: number): string {
  return n.toLocaleString();
}

interface DigitSlot {
  char: string;
  key: string;
  isDigit: boolean;
}

/**
 * Renders a number with per-digit rolling animation when the value changes.
 * Non-digit characters (commas, dots, $, k, M) render statically.
 */
export default function AnimatedNumber({
  value,
  format = defaultFormat,
  className = "",
  duration = 350,
}: AnimatedNumberProps) {
  const formatted = useMemo(() => format(value), [value, format]);
  const prevFormattedRef = useRef(formatted);
  const [slots, setSlots] = useState<DigitSlot[]>(() => buildSlots(formatted));
  const [animatingKeys, setAnimatingKeys] = useState<Set<string>>(new Set());

  useEffect(() => {
    const prev = prevFormattedRef.current;
    prevFormattedRef.current = formatted;

    if (prev === formatted) return;

    const newSlots = buildSlots(formatted);
    const prevSlots = buildSlots(prev);

    const changed = new Set<string>();
    newSlots.forEach((slot, i) => {
      const prevSlot = prevSlots[i];
      if (slot.isDigit && (!prevSlot || prevSlot.char !== slot.char)) {
        changed.add(slot.key);
      }
    });

    setSlots(newSlots);
    setAnimatingKeys(changed);

    if (changed.size > 0) {
      const timer = setTimeout(() => setAnimatingKeys(new Set()), duration);
      return () => clearTimeout(timer);
    }
  }, [formatted, duration]);

  return (
    <span className={`inline-flex items-baseline font-mono tabular-nums ${className}`}>
      {slots.map((slot) => {
        if (!slot.isDigit) {
          return (
            <span key={slot.key} className="inline-block">
              {slot.char}
            </span>
          );
        }
        const isAnimating = animatingKeys.has(slot.key);
        return (
          <span
            key={slot.key}
            className="inline-block overflow-hidden relative"
            style={{ width: "0.6em", height: "1.2em" }}
          >
            <span
              className="absolute inset-x-0 text-center"
              style={{
                transition: isAnimating
                  ? `transform ${duration}ms cubic-bezier(0.16, 1, 0.3, 1), opacity ${duration}ms ease-out`
                  : "none",
                transform: isAnimating ? "translateY(0)" : "translateY(0)",
                animation: isAnimating ? `digitRollIn ${duration}ms cubic-bezier(0.16, 1, 0.3, 1)` : "none",
              }}
            >
              {slot.char}
            </span>
          </span>
        );
      })}
    </span>
  );
}

function buildSlots(formatted: string): DigitSlot[] {
  const chars = formatted.split("");
  let digitIndex = 0;
  return chars.map((char, i) => {
    const isDigit = /\d/.test(char);
    if (isDigit) digitIndex++;
    return {
      char,
      key: isDigit ? `d${digitIndex}` : `s${i}`,
      isDigit,
    };
  });
}
