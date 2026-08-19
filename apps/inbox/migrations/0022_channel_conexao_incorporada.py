"""
F2.7 (§4.3.3): o canal passa a saber COMO foi ligado, e o `phone_number_id`
ganha unicidade.

⚠️ A constraint nova pode falhar em banco que já tenha dois canais vivos com o
mesmo `phone_number_id`. Isso é proposital: deduplicar sozinho escolheria por
conta própria de qual clínica é o número, que é exatamente a decisão que uma
migration não pode tomar. Para achar o conflito antes:

    SELECT phone_number_id, array_agg(clinic_id) FROM inbox_channel
     WHERE deleted_at IS NULL AND phone_number_id <> ''
     GROUP BY phone_number_id HAVING count(*) > 1;
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('inbox', '0021_template_waba_id'),
        ('tenants', '0006_remove_clinicbusinesshours_uniq_business_hours_weekday_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='channel',
            name='connected_at',
            field=models.DateTimeField(blank=True, null=True, verbose_name='Ligado em'),
        ),
        migrations.AddField(
            model_name='channel',
            name='connection_source',
            field=models.CharField(blank=True, choices=[('embedded_signup', 'Cadastro incorporado da Meta'), ('manual', 'Configurado à mão')], help_text='Vazio nos canais anteriores ao cadastro incorporado.', max_length=20, verbose_name='Ligado por'),
        ),
        migrations.AddField(
            model_name='channel',
            name='is_coexistence',
            field=models.BooleanField(default=False, help_text='RF-CON-3. O número segue no app do WhatsApp Business, então mensagem enviada de lá chega aqui como eco (RF-CON-5.1).', verbose_name='Também no celular'),
        ),
        migrations.AddField(
            model_name='channel',
            name='verified_name',
            field=models.CharField(blank=True, help_text='O nome que o paciente vê no WhatsApp, aprovado na Meta.', max_length=120, verbose_name='Nome verificado'),
        ),
        migrations.AddConstraint(
            model_name='channel',
            constraint=models.UniqueConstraint(condition=models.Q(('deleted_at__isnull', True), models.Q(('phone_number_id', ''), _negated=True)), fields=('phone_number_id',), name='uniq_channel_phone_number_id'),
        ),
    ]
