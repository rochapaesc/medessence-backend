# Ciclo de vida do atendimento (F2.5, §4.3.1) — o `needs_agent` booleano da
# F2 vira uma máquina de estados.
#
# ⚠️ A ORDEM importa: o `RemoveField` do `needs_agent` foi movido para o FIM,
# depois da conversão. Como o autogerador o colocou primeiro, o dado seria
# apagado antes de virar status e toda conversa da fila cairia como Aberta.

from django.db import migrations, models


def needs_agent_para_status(apps, schema_editor):
    """
    Converte o bit em estado:
      - `needs_agent=True`            → Aguardando (é o que a fila mostrava);
      - com responsável               → Aberta (alguém já estava atendendo);
      - o resto                       → Aguardando.

    Ninguém nasce Resolvida: seria declarar encerrado um trabalho que não foi
    feito, e a conversa sumiria da fila sem ninguém ter decidido isso.
    """
    Conversation = apps.get_model("inbox", "Conversation")

    Conversation.objects.filter(needs_agent=True).update(
        status="waiting", attended_by="none", assigned_to=None
    )
    Conversation.objects.filter(needs_agent=False, assigned_to__isnull=False).update(
        status="open", attended_by="agent"
    )
    Conversation.objects.filter(needs_agent=False, assigned_to__isnull=True).update(
        status="waiting", attended_by="none"
    )


def status_para_needs_agent(apps, schema_editor):
    Conversation = apps.get_model("inbox", "Conversation")
    Conversation.objects.filter(status="waiting").update(needs_agent=True)
    Conversation.objects.exclude(status="waiting").update(needs_agent=False)


class Migration(migrations.Migration):

    dependencies = [
        ('inbox', '0004_message_status_error'),
    ]

    operations = [
        migrations.AddField(
            model_name='conversation',
            name='attended_by',
            field=models.CharField(choices=[('none', 'Ninguém'), ('bot', 'IA'), ('agent', 'Atendente')], default='none', max_length=6, verbose_name='Atendida por'),
        ),
        migrations.AddField(
            model_name='conversation',
            name='attended_since',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Posse desde'),
        ),
        migrations.AddField(
            model_name='conversation',
            name='first_response_at',
            field=models.DateTimeField(blank=True, help_text='Primeira resposta HUMANA depois de um inbound (RF-ATD-11).', null=True, verbose_name='Primeira resposta em'),
        ),
        migrations.AddField(
            model_name='conversation',
            name='resolved_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Resolvida em'),
        ),
        migrations.AddField(
            model_name='conversation',
            name='snoozed_until',
            field=models.DateTimeField(blank=True, help_text='Volta sozinha para Aguardando nesta hora (RF-ATD-1.2).', null=True, verbose_name='Adiada até'),
        ),
        migrations.AddField(
            model_name='conversation',
            name='status',
            field=models.CharField(choices=[('waiting', 'Aguardando'), ('open', 'Aberta'), ('snoozed', 'Adiada'), ('resolved', 'Resolvida')], db_index=True, default='waiting', max_length=10, verbose_name='Status'),
        ),
        migrations.AddField(
            model_name='conversation',
            name='waiting_since',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Na fila desde'),
        ),
        migrations.AddField(
            model_name='message',
            name='activity_data',
            field=models.JSONField(blank=True, default=dict, help_text='Quem/para quem/resumo. O front monta a frase - backend que concatena texto engessa tradução e formatação.', verbose_name='Dados do evento'),
        ),
        migrations.AddField(
            model_name='message',
            name='activity_type',
            field=models.CharField(blank=True, choices=[('assigned', 'Assumiu o atendimento'), ('transferred', 'Transferiu'), ('resolved', 'Resolveu'), ('reopened', 'Reaberta'), ('snoozed', 'Adiou'), ('bot_started', 'IA assumiu'), ('bot_handoff', 'IA entregou para humano'), ('taken_over', 'Tomou o atendimento')], help_text='Preenchido só em evento de atividade (RF-ATD-4).', max_length=16, verbose_name='Tipo de evento'),
        ),
        migrations.AddField(
            model_name='message',
            name='is_internal',
            field=models.BooleanField(default=False, help_text='Anotação da equipe: NUNCA é enviada ao paciente (RF-ATD-3).', verbose_name='Nota interna'),
        ),
        migrations.AlterField(
            model_name='message',
            name='kind',
            field=models.CharField(choices=[('text', 'Texto'), ('image', 'Imagem'), ('audio', 'Áudio'), ('video', 'Vídeo'), ('document', 'Documento'), ('sticker', 'Figurinha'), ('location', 'Localização'), ('interactive', 'Interativa'), ('template', 'Template'), ('unsupported', 'Não suportado'), ('activity', 'Evento')], default='text', max_length=12, verbose_name='Tipo'),
        ),
        migrations.AlterField(
            model_name='message',
            name='sender_kind',
            field=models.CharField(choices=[('contact', 'Contato'), ('agent', 'Atendente'), ('bot', 'Automação'), ('system', 'Sistema')], max_length=8, verbose_name='Autor'),
        ),
        # Converte ANTES de apagar a origem do dado.
        migrations.RunPython(needs_agent_para_status, status_para_needs_agent),
        migrations.RemoveField(
            model_name='conversation',
            name='needs_agent',
        ),
    ]
