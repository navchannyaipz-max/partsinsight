"""Спільні налаштування: чорно-біла гама, шрифт із засічками (аналог Times New Roman), товщини ліній за ГОСТ."""
FONT = "FreeSerif"
HEAD = f'''graph [fontname="{FONT}", fontsize=14, bgcolor=white, pad=0.2];
node [fontname="{FONT}", fontsize=14, color=black, fontcolor=black, fillcolor=white, style=filled, penwidth=1.2];
edge [fontname="{FONT}", fontsize=12, color=black, fontcolor=black, penwidth=1.1, arrowsize=0.8];'''
