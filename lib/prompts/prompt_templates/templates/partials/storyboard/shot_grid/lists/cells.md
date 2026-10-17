{% for cell in cells %}
第{{ cell.number }}格{% if cell.shot_size %}（{{ cell.shot_size }}）{% endif %}：{{ cell.text }}
{% endfor %}
