#version 440
// Videobild aus Y'CbCr-Ebenen (NV12: Y + UV verschachtelt, YUV420P: Y + U + V) direkt auf der Grafikkarte nach RGB
layout(location = 0) in vec2 v_uv;
layout(location = 1) in vec4 v_color;
layout(location = 0) out vec4 fragColor;
layout(binding = 1) uniform sampler2D texY;
layout(binding = 2) uniform sampler2D texU;
layout(binding = 3) uniform sampler2D texV;
layout(std140, binding = 4) uniform cbuf { mat4 colorMatrix; float nv12; };
void main() {
    float y = texture(texY, v_uv).r;
    vec2 c = texture(texU, v_uv).rg;
    float v = nv12 > 0.5 ? c.g : texture(texV, v_uv).r;
    fragColor = vec4((colorMatrix * vec4(y, c.r, v, 1.0)).rgb, 1.0) * v_color;
}
