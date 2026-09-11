Classify the word "{{candidate.word}}".

If it is a greeting in any language (hello, hi, hola, bonjour, ...), kind is GREETING. Otherwise kind is OTHER.

Output **only** a JSON object, no prose before or after, exactly these fields:

{
  "kind": "GREETING" | "OTHER",
  "reason": "<one short sentence>"
}
