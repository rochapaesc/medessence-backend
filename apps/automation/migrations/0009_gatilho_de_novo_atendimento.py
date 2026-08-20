"""
RF-FLW-5.2: o gatilho "novo atendimento" entra ao lado da primeira mensagem.

Só acrescenta uma opção ao enum do campo (o Django guarda `choices` fora do
banco), então nenhum fluxo existente muda de comportamento.
"""

# Gerado por Django 5.2.12 on 2026-08-20 12:19

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('automation', '0008_sequence_exit_on_appointment_sequence_exit_on_reply'),
    ]

    operations = [
        migrations.AlterField(
            model_name='flow',
            name='trigger',
            field=models.CharField(choices=[('first_inbound', 'Primeira mensagem do contato'), ('new_conversation', 'Novo atendimento (conversa nova ou reaberta)'), ('keyword', 'Palavra-chave'), ('manual', 'Disparo manual do atendente')], default='first_inbound', max_length=20, verbose_name='Gatilho'),
        ),
    ]
