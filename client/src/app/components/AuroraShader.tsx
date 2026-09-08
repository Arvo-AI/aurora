"use client"

import { useEffect, useRef, useState } from "react"

const VERTEX_SHADER = `
attribute vec2 position;
void main() {
  gl_Position = vec4(position, 0.0, 1.0);
}
`

const FRAGMENT_SHADER = `
precision highp float;

uniform float iTime;
uniform vec2 iResolution;

mat2 rot(float a) { float c = cos(a), s = sin(a); return mat2(c, s, -s, c); }

float saw(float x) { return clamp(abs(fract(x) - 0.5), 0.02, 0.48); }
vec2 saw2(vec2 p) { return vec2(saw(p.x + saw(p.y)), saw(p.y + saw(p.x))); }

float curtainNoise(vec2 p, float flow) {
    float amp = 1.9;
    float gain = 2.2;
    float sum = 0.0;
    mat2 warp = mat2(0.93, 0.37, -0.37, 0.93);
    p *= rot(p.x * 0.04);
    vec2 q = p;
    for (int i = 0; i < 5; i++) {
        vec2 d = saw2(q * 1.5) * 0.9;
        d *= rot(iTime * flow + float(i) * 0.6);
        p += d / gain;
        q *= 1.25;
        gain *= 0.44;
        amp *= 0.46;
        p *= 1.15 + (sum - 1.0) * 0.018;
        sum += saw(p.x + saw(p.y)) * amp;
        p *= -warp;
    }
    return clamp(1.0 / pow(sum * 22.0, 1.25), 0.0, 0.6);
}

float hash(vec2 n) { return fract(sin(dot(n, vec2(41.1, 289.7))) * 43758.5); }

vec4 aurora(vec3 ro, vec3 rd) {
    vec4 col = vec4(0.0);
    vec4 avg = vec4(0.0);

    for (float i = 0.0; i < 50.0; i++) {
        float dither = 0.005 * hash(gl_FragCoord.xy + i) * smoothstep(0.0, 14.0, i);
        float h = ((0.7 + pow(i, 1.4) * 0.003) - ro.y) / (rd.y * 2.0 + 0.3);
        h -= dither;
        vec3 sp = ro + h * rd;

        float density = curtainNoise(sp.zx * 0.7, 0.065);
        vec4 sc = vec4(0.0, 0.0, 0.0, density);
        vec3 green = vec3(0.15, 0.9, 0.5);
        vec3 teal = vec3(0.1, 0.6, 0.8);
        vec3 purple = vec3(0.6, 0.15, 0.7);
        vec3 pink = vec3(0.85, 0.2, 0.45);
        float t = i / 50.0;
        vec3 c = mix(green, teal, smoothstep(0.0, 0.35, t));
        c = mix(c, purple, smoothstep(0.25, 0.6, t));
        c = mix(c, pink, smoothstep(0.5, 0.85, t));
        sc.rgb = c * density;
        avg = mix(avg, sc, 0.5);
        col += avg * exp2(-i * 0.055 - 2.2) * smoothstep(0.0, 4.0, i);
    }

    col *= clamp(rd.y * 12.0 + 0.5, 0.0, 1.0);
    return col * 2.2;
}

vec3 starHash(vec3 p) {
    p = fract(p * vec3(443.9, 397.3, 491.2));
    p += dot(p.zxy, p.yxz + 17.83);
    return fract(vec3(p.x * p.y, p.z * p.x, p.y * p.z));
}

vec3 stars(vec3 p) {
    vec3 c = vec3(0.0);
    float res = iResolution.x;
    for (float i = 0.0; i < 4.0; i++) {
        vec3 q = fract(p * (0.14 * res)) - 0.5;
        vec3 id = floor(p * (0.14 * res));
        vec2 rn = starHash(id).xy;
        float s = 1.0 - smoothstep(0.0, 0.55, length(q));
        s *= step(rn.x, 0.0005 + i * i * 0.001);
        c += s * (mix(vec3(1.0, 0.5, 0.15), vec3(0.7, 0.9, 1.0), rn.y) * 0.12 + 0.88);
        p *= 1.32;
    }
    return c * c * 0.75;
}

vec3 background(vec3 rd) {
    float d = dot(normalize(vec3(-0.5, -0.6, 0.9)), rd) * 0.5 + 0.5;
    d = pow(d, 5.0);
    return mix(vec3(0.05, 0.1, 0.2), vec3(0.1, 0.05, 0.2), d) * 0.6;
}

void main() {
    vec2 q = gl_FragCoord.xy / iResolution.xy;
    vec2 p = q - 0.5;
    p.x *= iResolution.x / iResolution.y;

    vec3 ro = vec3(0.0, 0.0, -6.7);
    vec3 rd = normalize(vec3(p, 1.3));
    vec2 look = vec2(-0.1, 0.1);
    look.x *= iResolution.x / iResolution.y;
    rd.yz *= rot(look.y);
    rd.xz *= rot(look.x + sin(iTime * 0.05) * 0.2);

    vec3 col = vec3(0.0);
    float fade = smoothstep(0.0, 0.01, abs(rd.y)) * 0.1 + 0.9;
    col = background(rd) * fade;

    if (rd.y > 0.0) {
        vec4 aur = smoothstep(0.0, 1.5, aurora(ro, rd)) * fade;
        col += stars(rd);
        col = col * (1.0 - aur.a) + aur.rgb;
    } else {
        rd.y = abs(rd.y);
        col = background(rd) * fade * 0.55;
        vec4 aur = smoothstep(0.0, 2.5, aurora(ro, rd));
        col += stars(rd) * 0.08;
        col = col * (1.0 - aur.a) + aur.rgb;
        vec3 rpos = ro + ((0.5 - ro.y) / rd.y) * rd;
        float nz = curtainNoise(rpos.xz * vec2(0.5, 0.7), 0.0);
        col += mix(vec3(0.2, 0.25, 0.5) * 0.07, vec3(0.3, 0.3, 0.5) * 0.65, nz * 0.4);
    }

    gl_FragColor = vec4(col, 1.0);
}
`

export default function AuroraShader({ className }: Readonly<{ className?: string }>) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const rafRef = useRef<number>(0)
  // When WebGL is unavailable (e.g. hardware acceleration disabled in the
  // browser), fall back to a static aurora gradient instead of a black void.
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return

    const gl = canvas.getContext("webgl", { alpha: true, antialias: false })
    if (!gl) {
      setFailed(true)
      return
    }

    const compile = (type: number, src: string) => {
      const s = gl.createShader(type)!
      gl.shaderSource(s, src)
      gl.compileShader(s)
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) {
        gl.deleteShader(s)
        return null
      }
      return s
    }

    const vs = compile(gl.VERTEX_SHADER, VERTEX_SHADER)
    const fs = compile(gl.FRAGMENT_SHADER, FRAGMENT_SHADER)
    if (!vs || !fs) {
      setFailed(true)
      return
    }

    const prog = gl.createProgram()!
    gl.attachShader(prog, vs)
    gl.attachShader(prog, fs)
    gl.linkProgram(prog)
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      gl.deleteProgram(prog)
      gl.deleteShader(vs)
      gl.deleteShader(fs)
      setFailed(true)
      return
    }
    gl.useProgram(prog)

    const buf = gl.createBuffer()
    gl.bindBuffer(gl.ARRAY_BUFFER, buf)
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1, 1,-1, -1,1, 1,1]), gl.STATIC_DRAW)
    const pos = gl.getAttribLocation(prog, "position")
    gl.enableVertexAttribArray(pos)
    gl.vertexAttribPointer(pos, 2, gl.FLOAT, false, 0, 0)

    const uTime = gl.getUniformLocation(prog, "iTime")
    const uRes = gl.getUniformLocation(prog, "iResolution")

    const resize = () => {
      const dpr = Math.min(window.devicePixelRatio, 1.5)
      canvas.width = canvas.clientWidth * dpr * 0.5
      canvas.height = canvas.clientHeight * dpr * 0.5
      gl.viewport(0, 0, canvas.width, canvas.height)
    }

    resize()
    window.addEventListener("resize", resize)

    const onContextLost = (e: Event) => {
      e.preventDefault()
      cancelAnimationFrame(rafRef.current)
      setFailed(true)
    }
    canvas.addEventListener("webglcontextlost", onContextLost)

    const start = performance.now()
    const loop = () => {
      const t = (performance.now() - start) / 1000
      gl.uniform1f(uTime, t)
      gl.uniform2f(uRes, canvas.width, canvas.height)
      gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4)
      rafRef.current = requestAnimationFrame(loop)
    }
    rafRef.current = requestAnimationFrame(loop)

    return () => {
      cancelAnimationFrame(rafRef.current)
      window.removeEventListener("resize", resize)
      canvas.removeEventListener("webglcontextlost", onContextLost)
      gl.deleteBuffer(buf)
      gl.deleteProgram(prog)
      gl.deleteShader(vs)
      gl.deleteShader(fs)
    }
  }, [])

  if (failed) {
    // WebGL is unavailable (no shader possible here) — paint a minimal, dark
    // aurora: near-black with a soft green→blue glow low on the horizon.
    return (
      <div
        className={className}
        style={{
          backgroundColor: "#05080a",
          backgroundImage: [
            // Glow lives in the upper/mid band so the sign-in page's dark
            // bottom overlay doesn't crush it. Alphas tuned to survive that overlay.
            "radial-gradient(120% 70% at 30% 30%, rgba(38,230,128,0.45) 0%, rgba(38,230,128,0) 55%)",
            "radial-gradient(130% 80% at 72% 40%, rgba(26,140,220,0.45) 0%, rgba(26,140,220,0) 55%)",
            "radial-gradient(100% 60% at 50% 10%, rgba(120,60,160,0.30) 0%, rgba(120,60,160,0) 60%)",
          ].join(", "),
        }}
      />
    )
  }

  return <canvas ref={canvasRef} className={className} />
}
