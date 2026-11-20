#version 440
// Striche (Freihand-Anmerkungen) als Dreiecke: Mittelpunkt + Auslenkung (halbe Strichbreite, Seitenkoordinaten).
// Der weiche Rand (1 Gerätepixel nach außen) und die Mindestbreite haarfeiner Striche werden hier in Gerätepixeln
// gerechnet – so passt dieselbe Form zu jeder Zoomstufe.
layout(location = 0) in vec2 center;
layout(location = 1) in vec2 ext;
layout(location = 2) in vec2 fz;      // x: 0 = Strichkante, 1 = Außenrand des Saums; y: Tiefe = Kennung der Anmerkung
layout(location = 3) in vec4 color;   // vormultipliziert (Deckkraft eingerechnet)
layout(location = 0) out vec4 v_color;
layout(std140, binding = 0) uniform buf { mat4 mvp; vec4 px; };  // px.xy: Normalkoordinaten je Gerätepixel
void main() {
    vec4 p = mvp * vec4(center + ext, 0.0, 1.0);
    vec2 d = (mvp * vec4(ext, 0.0, 0.0)).xy / px.xy;   // Auslenkung in Gerätepixeln
    float len = length(d);
    float a = 1.0;
    if (len > 1e-6) {
        vec2 dir = d / len;
        if (len < 0.5) {   // dünner als 1 Pixel: auf 1 Pixel verbreitern, dafür entsprechend blasser
            p.xy += dir * (0.5 - len) * px.xy;
            a = len / 0.5;
        }
        p.xy += dir * fz.x * px.xy;
    }
    v_color = color * (a * (1.0 - fz.x));
    gl_Position = vec4(p.xy, fz.y, 1.0);
}
