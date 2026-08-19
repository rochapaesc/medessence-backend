"""
F2.7 (§4.3.3, RF-CON-6): o contato passa a ter o identificador da Meta, e o
telefone deixa de ser obrigatório.

A troca da constraint do `wa_id` é para MAIS FROUXA (ela passa a valer só entre
contatos que TÊM número), então nenhum dado existente pode violá-la. Sem essa
mudança, o segundo contato sem telefone colidiria com o primeiro no vazio.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('patients', '0012_contact_marketing_opt_out'),
        ('tenants', '0006_remove_clinicbusinesshours_uniq_business_hours_weekday_and_more'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='contact',
            name='uniq_contact_wa_id',
        ),
        migrations.AddField(
            model_name='contact',
            name='user_id',
            field=models.CharField(blank=True, db_index=True, help_text="O BSUID, no formato 'BR.1234...' (RF-CON-6). Identifica a pessoa PARA ESTA EMPRESA e é o único caminho quando o telefone não vem. ⚠️ Ele MUDA quando a pessoa troca de telefone: é identificador de conversa, não identidade do paciente.", max_length=40, verbose_name='Identificador da Meta'),
        ),
        migrations.AlterField(
            model_name='contact',
            name='wa_id',
            field=models.CharField(blank=True, help_text="E.164 sem '+' (formato Meta). ⚠️ Pode vir VAZIO desde a F2.7: a Meta esconde o telefone de quem adota nome de usuário e não fala com a clínica há 30 dias (RF-CON-6).", max_length=20, verbose_name='Número (wa_id)'),
        ),
        migrations.AddConstraint(
            model_name='contact',
            constraint=models.UniqueConstraint(condition=models.Q(('deleted_at__isnull', True), models.Q(('wa_id', ''), _negated=True)), fields=('clinic', 'wa_id'), name='uniq_contact_wa_id'),
        ),
        migrations.AddConstraint(
            model_name='contact',
            constraint=models.UniqueConstraint(condition=models.Q(('deleted_at__isnull', True), models.Q(('user_id', ''), _negated=True)), fields=('clinic', 'user_id'), name='uniq_contact_user_id'),
        ),
    ]
